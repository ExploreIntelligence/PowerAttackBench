"""Host-side task manager for the Power Attacker SCADA platform.

This is the orchestration entry point (wrapped by ``./run_task.sh``). For every
task id it runs the full, isolated task lifecycle on the host:

  1. ``docker compose up --build -d``  (engine / scada / area01-05 / attacker)
  2. copy ``tasks/<task_id>/`` into the attacker container's ``/workspace``
  3. run Claude Code headless inside attacker with ``task.yaml`` as the prompt
  4. record model input/output traces (stream-json messages) to
     ``/opt/run-logs`` inside the attacker container
  5. stop when Claude Code exits or when ``--tasktime`` seconds have elapsed
  6. snapshot the engine (bus/branch/generator) at start and at end, then run
     the task evaluator and write the report into ``--evaluation-path``
  7. tear the stack down and copy the trace logs back to ``--trace-path``

Only the Python standard library is required on the host.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

HOST_DIR = Path(__file__).resolve().parent
PROJECT_DIR = HOST_DIR.parent
sys.path.insert(0, str(HOST_DIR))

import task_evaluator  # noqa: E402

COMPOSE_FILE = PROJECT_DIR / "docker-compose.yml"
TASKS_DIR = PROJECT_DIR / "tasks"
CASES_DIR = PROJECT_DIR / "cases"
CONTAINER_SERVICES = ["engine", "scada", "area01", "area02", "area03", "area04", "area05", "attacker"]
ATTACKER_WORKSPACE = "/workspace"
ATTACKER_LOG_DIR = "/opt/run-logs"

AREA_ORDER = ["areaA", "areaB", "areaC", "areaD", "areaE"]
# docker-compose env names for the per-area monitored-bus arguments.
AREA_ENV_NAMES = {name: f"AREA_{chr(ord('A') + idx)}_BUSES" for idx, name in enumerate(AREA_ORDER)}
DEFAULT_CASE = "ieee14"


def load_case_config(case_name: str) -> Dict[str, Any]:
    """Load and validate a cases/<name>.json file (host-side, stdlib only)."""
    path = CASES_DIR / f"{case_name}.json"
    if not path.is_file():
        available = ", ".join(sorted(p.stem for p in CASES_DIR.glob("*.json")))
        raise SystemExit(f"Unknown case {case_name!r}. Available cases: {available}")
    with open(path, "r", encoding="utf-8") as handle:
        config = json.load(handle)

    areas = config.get("areas")
    if not isinstance(areas, dict) or set(areas) != set(AREA_ORDER):
        raise SystemExit(f"case {case_name}: 'areas' must contain exactly {AREA_ORDER}")
    seen: Dict[int, str] = {}
    for area_name in AREA_ORDER:
        buses = areas[area_name]
        if not isinstance(buses, list) or not buses:
            raise SystemExit(f"case {case_name}: area {area_name} must be a non-empty list")
        for bus in buses:
            if not isinstance(bus, int) or bus <= 0:
                raise SystemExit(f"case {case_name}: invalid bus {bus!r} in {area_name}")
            if bus in seen:
                raise SystemExit(
                    f"case {case_name}: bus {bus} assigned to both {seen[bus]} and {area_name}"
                )
            seen[bus] = area_name
    bus_ids = sorted(seen)
    if bus_ids != list(range(1, len(bus_ids) + 1)):
        missing = sorted(set(range(1, len(bus_ids) + 1)) - set(bus_ids))
        raise SystemExit(f"case {case_name}: areas must cover buses 1..N; missing {missing}")
    if not isinstance(config.get("cmd_bus_base"), int):
        raise SystemExit(f"case {case_name}: 'cmd_bus_base' must be an integer")
    return config


def render_task_prompt(task_id: str, case_config: Dict[str, Any],
                       tasktime: Optional[int] = None) -> Optional[str]:
    """Render {{MARKER}} placeholders in tasks/<id>/task.yaml for the case.

    Returns the rendered prompt, or None when the task has no placeholders
    (blind tasks are topology-agnostic and copied verbatim).
    """
    template_path = TASKS_DIR / task_id / "task.yaml"
    template = template_path.read_text(encoding="utf-8")
    if "{{" not in template:
        return None
    if tasktime is None or tasktime <= 0:
        raise SystemExit(f"task {task_id}: a positive --tasktime is required to render the prompt")

    areas = case_config["areas"]
    base = int(case_config["cmd_bus_base"])
    area_c = areas["areaC"]
    trip_from, trip_to = 1, areas["areaA"][1]
    set_load_bus = areas["areaB"][0]
    bus_count = len([bus for buses in areas.values() for bus in buses])
    mapping_lines = "\n".join(f"- {name}: {', '.join(str(b) for b in areas[name])}"
                              for name in AREA_ORDER)
    markers = {
        "{{CASE_NAME}}": str(case_config["case_name"]),
        "{{CASE_DESCRIPTION}}": str(case_config.get("description", case_config["case_name"])),
        "{{TASK_TIME_SECONDS}}": str(tasktime),
        "{{TASK_TIME_MINUTES}}": (f"{tasktime // 60} minutes" if tasktime % 60 == 0
                                  else f"{tasktime / 60:.1f} minutes"),
        "{{BUS_COUNT}}": str(bus_count),
        "{{CMD_BUS_BASE}}": str(base),
        "{{AREA_MAPPING_LINES}}": mapping_lines,
        "{{AREA03_EXAMPLE_BUSES}}": f"{area_c[0]} and {area_c[1]}",
        "{{AREA03_EXAMPLE_BUS_0}}": str(area_c[0]),
        "{{AREA03_EXAMPLE_BUS_1}}": str(area_c[1]),
        "{{TRIP_EXAMPLE_FROM}}": str(trip_from),
        "{{TRIP_EXAMPLE_TO}}": str(trip_to),
        "{{TRIP_EXAMPLE_IOA}}": str(100000 + trip_from * base + trip_to),
        "{{SET_LOAD_EXAMPLE_BUS}}": str(set_load_bus),
        "{{SET_LOAD_EXAMPLE_PD_IOA}}": str(300000 + set_load_bus),
        "{{SET_LOAD_EXAMPLE_QD_IOA}}": str(400000 + set_load_bus),
    }
    rendered = template
    for marker, value in markers.items():
        rendered = rendered.replace(marker, value)
    if "{{" in rendered:
        unresolved = sorted({part[:part.index("}}") + 2]
                             for part in rendered.split("{{")[1:]})
        raise SystemExit(f"task {task_id}: unresolved template markers: {unresolved}")
    return rendered

# Runs inside the engine container; uses its preconfigured PYTHONPATH.
ENGINE_SNAPSHOT_SCRIPT = (
    "import json, sys\n"
    "from scada_core.iec104 import IEC104Client, IEC104Message\n"
    "reply = IEC104Client('127.0.0.1', 7001).send("
    "IEC104Message('command', {'action': 'snapshot'}))\n"
    "sys.stdout.write(json.dumps(reply))\n"
)

# Runs inside the attacker container via ``docker exec -i attacker bash -s``.
# The prompt file is task.yaml; the model IO is captured as stream-json
# (one JSON message per line). ``timeout`` enforces the task time budget.
ATTACKER_RUN_SCRIPT = r"""set -u
# The attacker container runs as root (needed for its iptables egress policy).
# IS_SANDBOX=1 is Claude Code's explicit opt-in for sandboxed/container
# environments; without it --dangerously-skip-permissions is refused as root.
export IS_SANDBOX=1
mkdir -p /opt/run-logs
# Claude Code first-run onboarding guard for non-interactive execution.
if [ ! -f "$HOME/.claude.json" ]; then
  printf '%s' '{"tosAccepted":true}' > "$HOME/.claude.json"
fi
cp -f /workspace/task.yaml /opt/run-logs/task.yaml
node -e 'const fs=require("fs");
const prompt=fs.readFileSync("/workspace/task.yaml","utf8");
fs.writeFileSync("/opt/run-logs/trace.jsonl",
  JSON.stringify({type:"user_input", role:"user", task_id:process.env.TASK_ID,
                  case_name:process.env.CASE_NAME,
                  timestamp:process.env.RUN_TS, message:prompt})+"\n");'
# Keep the target-case identity out of the agent's inherited environment.
unset CASE_NAME
START_EPOCH=$(date +%s)
set +e
# stdin must come from /dev/null: this script itself arrives on stdin
# (docker exec -i ... bash -s), and claude would otherwise consume the
# remaining script lines as its own input.
timeout --signal=TERM --kill-after=15s "${TASKTIME}s" \
  claude -p "$(cat /workspace/task.yaml)" \
  --output-format stream-json --verbose \
  --dangerously-skip-permissions \
  < /dev/null \
  >> /opt/run-logs/trace.jsonl 2>> /opt/run-logs/claude.stderr.log
CODE=$?
set -u
END_EPOCH=$(date +%s)
if [ "$CODE" = "124" ] || [ "$CODE" = "137" ]; then
  REASON="time_limit"
else
  REASON="completed"
fi
printf 'exit_code=%s\nstart_epoch=%s\nend_epoch=%s\nreason=%s\n' \
  "$CODE" "$START_EPOCH" "$END_EPOCH" "$REASON" > /opt/run-logs/run_meta.env
exit 0
"""


def log(message: str) -> None:
    print(f"[task-manager {datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


def run_cmd(
    cmd: Sequence[str],
    *,
    input_bytes: Optional[bytes] = None,
    timeout: Optional[float] = None,
    check: bool = True,
    capture: bool = False,
    env: Optional[Dict[str, str]] = None,
    cwd: Optional[Path] = None,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(cmd),
        input=input_bytes,
        timeout=timeout,
        check=check,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        env=env,
        cwd=str(cwd) if cwd is not None else None,
    )


def docker(*args: str, **kwargs: Any) -> subprocess.CompletedProcess:
    return run_cmd(["docker", *args], **kwargs)


def compose(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return run_cmd(
        ["docker", "compose", "-f", str(COMPOSE_FILE), *args],
        cwd=PROJECT_DIR,
        check=check,
    )


def all_images_exist() -> bool:
    """True if every service in docker-compose.yml already has a built image.

    Uses ``docker compose config --images`` (lists the expected image name of
    each service without depending on container state — ``docker compose images``
    fails once the stack is down or when unrelated stale containers confuse it)
    and then verifies each name with ``docker image inspect``.
    """
    listed = run_cmd(
        ["docker", "compose", "-f", str(COMPOSE_FILE), "config", "--images"],
        cwd=PROJECT_DIR, check=False, capture=True,
    )
    if listed.returncode != 0:
        return False
    image_names = [line.strip() for line in listed.stdout.decode().splitlines() if line.strip()]
    if not image_names:
        return False
    for name in image_names:
        inspect = run_cmd(
            ["docker", "image", "inspect", name],
            cwd=PROJECT_DIR, check=False, capture=True,
        )
        if inspect.returncode != 0:
            return False
    return True


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Power Attacker host-side task manager / evaluator entry point.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "taskid_positional",
        nargs="?",
        help="Task id, e.g. t-01 (comma-separated for multiple tasks: t-01,t-02).",
    )
    parser.add_argument("--taskid", dest="taskid_opt", help="Task id (alternative to the positional argument).")
    parser.add_argument("--case", dest="case_name", default=DEFAULT_CASE,
                        help="Target power-system case name under cases/ (ieee14/ieee39/ieee57/ieee118).")
    parser.add_argument("--tasktime", type=int, default=600, help="Task time budget in seconds.")
    parser.add_argument("--trace-path", default="traces", help="Host directory for model IO traces.")
    parser.add_argument("--evaluation-path", default="evaluations", help="Host directory for evaluation reports.")
    parser.add_argument("--no-build", action="store_true",
                        help="Never build; fail if images are missing (docker compose up -d).")
    parser.add_argument("--force-build", action="store_true",
                        help="Force a full image rebuild, even if the images already exist.")
    parser.add_argument("--keep-containers", action="store_true",
                        help="Keep the stack running after each task (for debugging).")
    parser.add_argument("--ready-timeout", type=int, default=180,
                        help="Seconds to wait for the stack to become ready.")
    args = parser.parse_args(argv)
    args.taskid = args.taskid_opt or args.taskid_positional
    if not args.taskid:
        parser.error("task id is required (positional or --taskid), e.g. ./run_task.sh t-01 --tasktime 600")
    if args.tasktime <= 0:
        parser.error("--tasktime must be a positive number of seconds")
    return args


def preflight(task_ids: List[str], case_name: str) -> Dict[str, Any]:
    try:
        docker("ps", capture=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise SystemExit(f"Docker is not accessible from this shell: {exc}")

    # Validate the target case config up front (also fails fast on typos).
    case_config = load_case_config(case_name)

    # docker compose loads .env itself, so accept either exported vars or .env.
    env_file = PROJECT_DIR / ".env"
    has_credential = bool(os.environ.get("ANTHROPIC_AUTH_TOKEN")
                          or os.environ.get("ANTHROPIC_API_KEY"))
    missing = [name for name in ("ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL")
               if not os.environ.get(name)]
    if (missing or not has_credential) and not env_file.exists():
        raise SystemExit(
            "Missing ANTHROPIC configuration. Provide ANTHROPIC_BASE_URL, "
            "ANTHROPIC_MODEL and a credential (ANTHROPIC_AUTH_TOKEN for the "
            "Authorization: Bearer header, or ANTHROPIC_API_KEY for x-api-key), "
            "either as exports or by copying .env.example to .env."
        )

    for task_id in task_ids:
        task_dir = TASKS_DIR / task_id
        if not task_dir.is_dir():
            raise SystemExit(f"Task directory not found: {task_dir}")
        if not (task_dir / "task.yaml").is_file():
            raise SystemExit(f"task.yaml not found in {task_dir}")
    return case_config


def wait_until_ready(timeout_seconds: int) -> None:
    deadline = time.time() + timeout_seconds
    log("waiting for all containers to enter running state ...")
    while time.time() < deadline:
        result = docker("inspect", "-f", "{{.State.Running}}", *CONTAINER_SERVICES,
                        capture=True, check=False)
        if result.returncode == 0:
            states = result.stdout.decode().split()
            if len(states) == len(CONTAINER_SERVICES) and all(state == "true" for state in states):
                break
        time.sleep(2)
    else:
        raise RuntimeError("containers did not become all-running before the ready timeout")

    log("waiting for the engine snapshot service (10.10.0.100:7001) ...")
    while time.time() < deadline:
        try:
            fetch_engine_snapshot(timeout=10)
            break
        except Exception:
            time.sleep(2)
    else:
        raise RuntimeError("engine did not answer snapshot requests before the ready timeout")

    log("waiting for SCADA ssh port 10.10.0.10:22 from the attacker container ...")
    probe = "timeout 3 bash -c 'exec 3<>/dev/tcp/10.10.0.10/22'"
    while time.time() < deadline:
        result = docker("exec", "attacker", "bash", "-c", probe, capture=True, check=False)
        if result.returncode == 0:
            break
        time.sleep(2)
    else:
        raise RuntimeError("SCADA ssh port was not reachable before the ready timeout")


def fetch_engine_snapshot(timeout: float = 15.0) -> Dict[str, Any]:
    result = docker("exec", "-i", "engine", "python3", "-",
                    input_bytes=ENGINE_SNAPSHOT_SCRIPT.encode("utf-8"),
                    capture=True, timeout=timeout)
    return json.loads(result.stdout.decode("utf-8"))


def save_snapshot(snapshot: Dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(snapshot, handle, ensure_ascii=False, indent=2)


def force_stop_attacker_agent() -> None:
    """Best-effort cleanup if the host-side backstop timeout ever fires."""
    log("host-side time backstop fired, terminating claude inside the attacker container ...")
    kill_script = (
        "for p in /proc/[0-9]*; do "
        "c=$(tr '\\0' ' ' < $p/cmdline 2>/dev/null || true); "
        'case "$c" in *claude*) kill -TERM "${p##*/}" 2>/dev/null || true;; esac; '
        "done"
    )
    docker("exec", "attacker", "bash", "-c", kill_script, capture=True, check=False)


def run_attacker(task_id: str, tasktime: int, case_name: str) -> Tuple[str, Dict[str, str], float]:
    """Launch Claude Code headless in the attacker container.

    Returns (termination_reason, run_meta, wall_seconds).
    """
    run_ts = datetime.now().isoformat(timespec="seconds")
    env = dict(os.environ, TASKTIME=str(tasktime), TASK_ID=task_id,
               CASE_NAME=case_name, RUN_TS=run_ts)
    start = time.time()
    try:
        run_cmd(
            ["docker", "exec", "-i",
             "-e", "TASKTIME", "-e", "TASK_ID", "-e", "CASE_NAME", "-e", "RUN_TS",
             "attacker", "bash", "-s"],
            input_bytes=ATTACKER_RUN_SCRIPT.encode("utf-8"),
            timeout=tasktime + 120,
            env=env,
        )
    except subprocess.TimeoutExpired:
        force_stop_attacker_agent()
    wall_seconds = time.time() - start

    meta_raw = docker("exec", "attacker", "cat", f"{ATTACKER_LOG_DIR}/run_meta.env",
                      capture=True, check=False)
    meta: Dict[str, str] = {}
    if meta_raw.returncode == 0:
        for line in meta_raw.stdout.decode().splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                meta[key.strip()] = value.strip()
    reason = meta.get("reason", "unknown")
    return reason, meta, wall_seconds


def collect_traces(trace_dir: Path) -> None:
    trace_dir.mkdir(parents=True, exist_ok=True)
    docker("cp", f"attacker:{ATTACKER_LOG_DIR}/.", str(trace_dir))


def run_task(task_id: str, args: argparse.Namespace, run_tag: str,
             case_config: Dict[str, Any]) -> bool:
    log(f"===== task {task_id} started (case {args.case_name}, run tag {run_tag}) =====")
    trace_dir = Path(args.trace_path) / run_tag
    eval_dir = Path(args.evaluation_path) / run_tag
    trace_dir.mkdir(parents=True, exist_ok=True)
    eval_dir.mkdir(parents=True, exist_ok=True)

    start_snapshot: Optional[Dict[str, Any]] = None
    end_snapshot: Optional[Dict[str, Any]] = None
    termination = "unknown"
    duration = 0.0
    success = False

    try:
        # Load per-task stack env overrides (e.g., defense settings like
        # AUTORECLOSE_DELAY) before starting the stack. These are picked up by
        # docker-compose ${VAR:-default} interpolation.
        stack_env = TASKS_DIR / task_id / "stack.env"
        if stack_env.is_file():
            for line in stack_env.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, val = line.partition("=")
                    os.environ[key.strip()] = val.strip()
                    log(f"  stack env: {key.strip()}={val.strip()}")

        # Case wiring: POWER_CASE selects cases/<name>.json inside every
        # service; AREA_X_BUSES parameterizes the C server argv in compose;
        # POWER_LINES carries the commissioned point table (directed branch
        # pairs) the C server validates trip/close IOAs against.
        os.environ["POWER_CASE"] = args.case_name
        os.environ["POWER_LINES"] = ",".join(case_config["lines"])
        for area_name in AREA_ORDER:
            buses = ",".join(str(bus) for bus in case_config["areas"][area_name])
            os.environ[AREA_ENV_NAMES[area_name]] = buses
        log(f"  target case: {args.case_name} ({case_config.get('pypower_case')})")

        # step 1: build and start the whole stack for this task.
        # Default: build only if images are missing; reuse existing images
        # on subsequent runs. --force-build rebuilds unconditionally; --no-build
        # never builds (fails if images are missing).
        up_args = ["up", "-d"]
        if args.force_build:
            log("step 1/7: docker compose up --build (force-build requested) ...")
            up_args.insert(1, "--build")
        elif args.no_build:
            log("step 1/7: docker compose up -d (--no-build; reusing images) ...")
        elif all_images_exist():
            log("step 1/7: docker compose up -d (images exist; skipping build) ...")
        else:
            log("step 1/7: docker compose up --build (images missing; first build) ...")
            up_args.insert(1, "--build")
        compose(*up_args)

        wait_until_ready(args.ready_timeout)

        # step 6a: engine snapshot at task start (before the agent acts).
        log("recording engine snapshot at task start ...")
        start_snapshot = fetch_engine_snapshot()
        save_snapshot(start_snapshot, eval_dir / "snapshot_start.json")

        # step 2: copy task files into the attacker workspace.
        log(f"step 2/7: copying tasks/{task_id}/ -> attacker:{ATTACKER_WORKSPACE}/ ...")
        docker("cp", f"{TASKS_DIR / task_id}/.", f"attacker:{ATTACKER_WORKSPACE}/")
        # stack.env holds host-side stack overrides (e.g. t-04 defense params);
        # it must never land in the agent's workspace.
        docker("exec", "attacker", "rm", "-f", f"{ATTACKER_WORKSPACE}/stack.env")
        docker("exec", "attacker", "mkdir", "-p", ATTACKER_LOG_DIR)

        # Render case-specific placeholders (t-01 full-disclosure brief;
        # t-02 also renders its area map; t-03/t-04 have no placeholders).
        rendered_prompt = render_task_prompt(task_id, case_config, args.tasktime)
        if rendered_prompt is not None:
            with tempfile.NamedTemporaryFile("w", suffix="-task.yaml", delete=False,
                                             encoding="utf-8") as handle:
                handle.write(rendered_prompt)
                rendered_path = handle.name
            try:
                docker("cp", rendered_path, f"attacker:{ATTACKER_WORKSPACE}/task.yaml")
            finally:
                os.unlink(rendered_path)
            log("  rendered task.yaml for the target case")

        # step 3-5: run claude code with task.yaml, traces captured, time-boxed.
        log(f"step 3-5/7: running claude code in attacker (time budget {args.tasktime}s) ...")
        termination, _meta, duration = run_attacker(task_id, args.tasktime, args.case_name)
        log(f"claude code finished: termination={termination}, wall={duration:.1f}s")

        # step 6b: engine snapshot at task end.
        log("recording engine snapshot at task end ...")
        try:
            end_snapshot = fetch_engine_snapshot()
        except Exception as exc:
            log(f"WARNING: could not fetch end snapshot: {exc}")
        if end_snapshot is not None:
            save_snapshot(end_snapshot, eval_dir / "snapshot_end.json")

        # step 7a: pull the traces out before tearing the stack down.
        log(f"step 7/7: collecting traces -> {trace_dir} ...")
        collect_traces(trace_dir)

        # step 6c: evaluate and write the report on the host.
        if end_snapshot is not None:
            log("running task evaluator ...")
            report = task_evaluator.evaluate(
                start_snapshot, end_snapshot, task_id, duration, termination,
                case_name=args.case_name,
                case_description=case_config.get("description"),
            )
            paths = task_evaluator.write_reports(report, eval_dir)
            score = report["result"]["score"]
            log(f"damage score: {score['damage_score']}/100 ({score['severity']})")
            log(f"evaluation report: {paths['markdown']}")
        else:
            log("evaluation skipped because the end snapshot is unavailable")

        success = True
    except KeyboardInterrupt:
        log("interrupted by user")
        raise
    except Exception as exc:
        log(f"task {task_id} failed: {exc}")
    finally:
        if not args.keep_containers:
            log("tearing the stack down (docker compose down --remove-orphans) ...")
            compose("down", "--remove-orphans", check=False)
        else:
            log("--keep-containers set; leaving the stack running")
        log(f"===== task {task_id} finished =====")
    return success


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    task_ids = [item.strip() for item in args.taskid.split(",") if item.strip()]
    case_config = preflight(task_ids, args.case_name)

    Path(args.trace_path).mkdir(parents=True, exist_ok=True)
    Path(args.evaluation_path).mkdir(parents=True, exist_ok=True)

    failures: List[str] = []
    for index, task_id in enumerate(task_ids):
        run_tag = f"{task_id}-{args.case_name}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        try:
            ok = run_task(task_id, args, run_tag, case_config)
        except KeyboardInterrupt:
            log("aborting remaining tasks")
            if not args.keep_containers:
                compose("down", "--remove-orphans", check=False)
            return 130
        if not ok:
            failures.append(task_id)

    if failures:
        log(f"completed with failures: {', '.join(failures)}")
        return 1
    log(f"all tasks completed: {', '.join(task_ids)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
