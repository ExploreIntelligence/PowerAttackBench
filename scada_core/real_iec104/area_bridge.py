from __future__ import annotations

import argparse
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from ..iec104 import IEC104Client, IEC104Message
from ..power_model import AREA_LAYOUT, PowerFlowEngine, PowerFlowUnsolvableError


# IEC 60870-5-101 quality descriptor (QDS) bits published with every measurement.
QDS_GOOD = 0x00
QDS_INVALID = 0x80  # IV: value invalid / non-topical


def build_area_state(full: dict[str, Any], area_name: str) -> dict[str, Any]:
    """Project an engine snapshot onto one area's published SCADA state.

    Telemetry semantics when the engine cannot solve a steady state:
      * buses disconnected from the slack (de-energized islands) publish zero
        with GOOD quality -- a trustworthy physical zero (V=0, no flow);
      * buses still connected to the slack publish the last converged values
        with the INVALID quality bit -- stale/non-topical estimated data.
    When solved, every value is live with GOOD quality.
    """
    buses = [bus for bus in full["buses"] if bus["bus"] in AREA_LAYOUT[area_name]]
    branches = [
        branch
        for branch in full["branches"]
        if branch["from_bus"] in AREA_LAYOUT[area_name]
        or branch["to_bus"] in AREA_LAYOUT[area_name]
    ]
    generators = [gen for gen in full["generators"] if gen["bus"] in AREA_LAYOUT[area_name]]

    solved = full.get("system_status") == "solved"
    energized_list = full.get("energized_buses")
    if energized_list is None:
        energized = {bus["bus"] for bus in buses}
    else:
        energized = set(energized_list)

    snapshot: dict[str, Any] = {
        "area": area_name,
        "system_status": full["system_status"],
        "collapsed": bool(full.get("collapsed", False)),
        "collapse_reason": full.get("collapse_reason"),
        "buses": buses,
        "branches": branches,
        "generators": generators,
    }
    total_pd = total_qd = total_pg = 0.0
    for bus in buses:
        bus_id = bus["bus"]
        pg = sum(gen["Pg"] for gen in generators if gen["bus"] == bus_id)
        if bus_id not in energized:
            vm_scaled = 0
            pd = qd = pg_value = 0.0
            quality = QDS_GOOD
        elif not solved:
            vm_scaled = int(round(bus["Vm"] * 1000))
            pd, qd, pg_value = bus["Pd"], bus["Qd"], pg
            quality = QDS_INVALID
        else:
            vm_scaled = int(round(bus["Vm"] * 1000))
            pd, qd, pg_value = bus["Pd"], bus["Qd"], pg
            quality = QDS_GOOD
        snapshot[f"bus_{bus_id}_vm"] = vm_scaled
        snapshot[f"bus_{bus_id}_pd"] = pd
        snapshot[f"bus_{bus_id}_qd"] = qd
        snapshot[f"bus_{bus_id}_pg"] = pg_value
        snapshot[f"bus_{bus_id}_q"] = quality
        total_pd += pd
        total_qd += qd
        total_pg += pg_value
    snapshot["total_pd"] = total_pd
    snapshot["total_qd"] = total_qd
    snapshot["generator_pg"] = total_pg
    snapshot["telemetry_value"] = snapshot.get(
        f"bus_{buses[0]['bus']}_vm", 0
    ) if buses else 0
    return snapshot


def area_snapshot(engine: PowerFlowEngine, area_name: str) -> dict[str, Any]:
    return build_area_state(engine.snapshot(), area_name)


def write_state(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def apply_command(engine: PowerFlowEngine, command: dict[str, Any]) -> None:
    action = command["action"]
    if action == "trip_line":
        engine.trip_line(command["from_bus"], command["to_bus"])
    elif action == "close_line":
        engine.close_line(command["from_bus"], command["to_bus"])
    elif action == "set_load":
        engine.set_load(command["bus_id"], command["pd"], command["qd"])
    else:
        raise ValueError(f"Unsupported action: {action}")


class AutoRecloseGuard:
    """Schedules automatic close_line commands after trip_line commands.

    Enabled only when AUTORECLOSE_DELAY > 0 (env var, seconds). Each tripped
    branch gets a daemon timer that sends close_line back to the engine after
    the delay. Explicit close_line cancels the pending timer.
    """

    def __init__(self, engine_client: IEC104Client | None, area_name: str, delay: float):
        self._client = engine_client
        self._area = area_name
        self._delay = delay
        self._timers: dict[tuple[int, int], threading.Timer] = {}
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self._delay > 0 and self._client is not None

    def arm(self, from_bus: int, to_bus: int) -> None:
        if not self.enabled:
            return
        key = (from_bus, to_bus)
        with self._lock:
            old = self._timers.pop(key, None)
            if old:
                old.cancel()
            timer = threading.Timer(self._delay, self._fire, args=(from_bus, to_bus))
            timer.daemon = True
            self._timers[key] = timer
            timer.start()

    def disarm(self, from_bus: int, to_bus: int) -> None:
        key = (from_bus, to_bus)
        with self._lock:
            old = self._timers.pop(key, None)
            if old:
                old.cancel()

    def disarm_all(self) -> None:
        """Cancel every pending reclose (used once the grid has collapsed)."""
        with self._lock:
            pending = list(self._timers.values())
            self._timers.clear()
        for timer in pending:
            timer.cancel()

    def _fire(self, from_bus: int, to_bus: int) -> None:
        try:
            reply = self._client.send(IEC104Message("command", {"action": "close_line", "from_bus": from_bus, "to_bus": to_bus}))
            if isinstance(reply, dict) and reply.get("status") == "rejected":
                # Latch already engaged at the engine: the line must stay open.
                print(f"[bridge:{self._area}] auto-reclose {from_bus}-{to_bus} rejected: {reply.get('reason')}", flush=True)
            else:
                print(f"[bridge:{self._area}] auto-reclose {from_bus}-{to_bus}", flush=True)
        except Exception as exc:
            print(f"[bridge:{self._area}] auto-reclose failed: {exc}", flush=True)
        with self._lock:
            self._timers.pop((from_bus, to_bus), None)


def run(area_name: str, state_path: Path, command_path: Path, interval: float, engine_target: str | None) -> None:
    engine = PowerFlowEngine() if engine_target is None else None
    remote_client = None
    if engine_target is not None:
        host, port = engine_target.split(":", 1)
        remote_client = IEC104Client(host, int(port))
    autoreclose = AutoRecloseGuard(
        remote_client, area_name, float(os.environ.get("AUTORECLOSE_DELAY", "0"))
    )
    offset = 0
    collapsed = False
    state_path.parent.mkdir(parents=True, exist_ok=True)
    command_path.touch(exist_ok=True)
    while True:
        with command_path.open("r", encoding="utf-8") as command_file:
            command_file.seek(offset)
            while True:
                line = command_file.readline()
                if not line:
                    break
                offset = command_file.tell()
                line = line.strip()
                if not line:
                    continue
                try:
                    command = json.loads(line)
                    action = command.get("action")
                    # Latched collapse: controls may still be sent by a master
                    # but must not reach the engine anymore.
                    if collapsed and action in ("trip_line", "close_line", "set_load"):
                        print(f"[bridge:{area_name}] skipped {action}: system collapsed", flush=True)
                        continue
                    if remote_client is None:
                        try:
                            apply_command(engine, command)
                        except PowerFlowUnsolvableError:
                            # The mutation took effect but the grid is now
                            # unsolved in a recoverable (local) island: treat
                            # the command as applied so reclose still gets armed.
                            pass
                    else:
                        reply = remote_client.send(IEC104Message("command", command))
                        if isinstance(reply, dict) and reply.get("status") == "rejected":
                            print(
                                f"[bridge:{area_name}] command rejected: {reply.get('reason')}",
                                flush=True,
                            )
                            continue
                    if action == "trip_line":
                        autoreclose.arm(command["from_bus"], command["to_bus"])
                    elif action == "close_line":
                        autoreclose.disarm(command["from_bus"], command["to_bus"])
                    print(f"[bridge:{area_name}] applied {line}", flush=True)
                except Exception as error:
                    print(f"[bridge:{area_name}] command failed: {error}", flush=True)
        if remote_client is None:
            snapshot = area_snapshot(engine, area_name)
        else:
            response = remote_client.send(IEC104Message("command", {"action": "snapshot"}))
            snapshot = build_area_state(response, area_name)
        write_state(state_path, snapshot)
        # The collapse latch is global at the engine. Once observed, stop
        # arming reclose timers: the grid must remain in its collapsed state.
        collapsed = bool(snapshot.get("collapsed"))
        if collapsed:
            autoreclose.disarm_all()
        time.sleep(interval)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("area_name")
    parser.add_argument("--state", required=True)
    parser.add_argument("--commands", required=True)
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--engine", default=None, help="engine host:port for the shared PYPOWER state")
    args = parser.parse_args()
    if args.area_name not in AREA_LAYOUT:
        raise ValueError(f"Unknown area {args.area_name}")
    run(args.area_name, Path(args.state), Path(args.commands), args.interval, args.engine)


if __name__ == "__main__":
    main()
