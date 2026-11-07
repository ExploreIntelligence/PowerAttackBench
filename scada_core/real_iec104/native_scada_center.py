from __future__ import annotations

import os
import json
import subprocess
import sys
import time

from ..iec104 import IEC104Client, IEC104Message
from ..power_model import AREA_LAYOUT


TARGETS = [
    ("area01", "10.10.0.2"),
    ("area02", "10.10.0.3"),
    ("area03", "10.10.0.4"),
    ("area04", "10.10.0.5"),
    ("area05", "10.10.0.6"),
]


AREA_HOSTS = {area: host for area, host in TARGETS}
AREA_HOSTS.update({f"area{index:02d}": host for index, (_, host) in enumerate(TARGETS, start=1)})
AREA_HOSTS.update({area: host for area, (_, host) in zip(("areaA", "areaB", "areaC", "areaD", "areaE"), TARGETS)})


def run_client(host: str, ioa: int = 0, action: str | None = None, values: list[str] | None = None) -> bool:
    client = os.environ.get("SCADA_CLIENT", "/app/power_attacker/real_iec104/scada_client")
    if action is not None:
        command = [client, host, "2404", action, *(values or [])]
    else:
        command = [client, host, "2404", str(ioa), "1"]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    for line in (result.stdout + result.stderr).splitlines():
        print(f"[scada] {host} {line}", flush=True)
    return result.returncode == 0


def engine_snapshot() -> dict:
    host = os.environ.get("ENGINE_HOST", "10.10.0.100")
    port = int(os.environ.get("ENGINE_PORT", "7001"))
    return IEC104Client(host, port).send(IEC104Message("command", {"action": "snapshot"}))


def area_host(area: str) -> str:
    try:
        return AREA_HOSTS[area]
    except KeyError as error:
        raise ValueError(f"unknown area {area!r}; use area01-area05 or areaA-areaE") from error


def area_for_bus(bus_id: int) -> str:
    for area, buses in AREA_LAYOUT.items():
        if bus_id in buses:
            return area
    max_bus = max(bus for buses in AREA_LAYOUT.values() for bus in buses)
    raise ValueError(
        f"unknown bus {bus_id}; expected a bus from 1 to {max_bus} "
        f"for the configured case"
    )


def interactive_controller() -> None:
    connected = False
    print("SCADA controller ready. Type help for commands.", flush=True)
    while True:
        try:
            line = input("scada> ").strip()
        except EOFError:
            return
        if not line:
            continue
        parts = line.split()
        command = parts[0].lower()
        try:
            if command == "connect":
                for _, host in TARGETS:
                    run_client(host)
                connected = True
                print({"status": "connected", "areas": list(AREA_HOSTS)}, flush=True)
            elif command == "disconnect":
                connected = False
                print({"status": "disconnected"}, flush=True)
            elif command in {"trip_line", "close_line"}:
                if len(parts) == 3:
                    from_bus, to_bus = (int(value) for value in parts[1:])
                    host = area_host(area_for_bus(from_bus))
                    values = [str(from_bus), str(to_bus)]
                elif len(parts) == 4:
                    host = area_host(parts[1])
                    values = parts[2:]
                else:
                    print(f"Usage: {command} <from_bus> <to_bus>", flush=True)
                    continue
                if run_client(host, action="trip" if command == "trip_line" else "close", values=values):
                    print({"status": "ok", "command": command}, flush=True)
            elif command == "set_load":
                if len(parts) != 5:
                    print(f"Usage: set_load <area> <bus_id> <pd> <qd>", flush=True)
                    continue
                owning_host = area_host(area_for_bus(int(parts[2])))
                if area_host(parts[1]) != owning_host:
                    print(f"[scada] rejected: bus {parts[2]} is not owned by {parts[1]}", flush=True)
                    continue
                if run_client(area_host(parts[1]), action="set_load", values=parts[2:]):
                    print({"status": "ok", "command": command}, flush=True)
            elif command in {"branch", "bus", "generator"}:
                snapshot = engine_snapshot()
                field = {"branch": "branches", "bus": "buses", "generator": "generators"}[command]
                print(json.dumps(snapshot[field], indent=2), flush=True)
            elif command == "help":
                print("connect | disconnect | trip_line <from_bus> <to_bus> | "
                    "close_line <from_bus> <to_bus> | set_load <area> <bus> <pd> <qd> | "
                      "branch | bus | generator | exit", flush=True)
            elif command == "exit":
                return
            else:
                print(f"unknown command: {command}", flush=True)
        except (ValueError, ConnectionError, OSError) as error:
            print(f"[scada] command failed: {error}", flush=True)


def main() -> int:
    if os.geteuid() != 0:
        print("SCADA center must be run as root", file=sys.stderr, flush=True)
        return 1

    if sys.stdin.isatty() and os.environ.get("SCADA_INTERACTIVE", "1") != "0":
        interactive_controller()
        return 0

    interval = float(os.environ.get("SCADA_POLL_INTERVAL", "8"))
    while True:
        for area, host in TARGETS:
            run_client(host)
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
