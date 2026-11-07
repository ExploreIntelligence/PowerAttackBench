from __future__ import annotations

import argparse
import json
import socket
import sys
import threading
import time
from typing import Any, Dict, List, Tuple

from .iec104 import IEC104Client, IEC104Message, IEC104Server
from .power_model import AREA_LAYOUT, PowerFlowEngine, PowerFlowUnsolvableError
from .scada import SCADAController


class EngineService:
    def __init__(self, host: str, port: int):
        self.engine = PowerFlowEngine()
        self.host = host
        self.port = port

    @staticmethod
    def _control_reply(message: str, engine: PowerFlowEngine) -> Dict[str, Any]:
        # A command is always reported as applied once the engine accepted the
        # mutation -- including one that leaves the grid unsolved (a local
        # island). The reply carries the post-command state so bridges can tell
        # an unsolved-but-recoverable grid from a freshly latched collapse;
        # later commands against a latched engine are rejected separately.
        return {
            "status": "ok",
            "message": message,
            "system_status": engine.system_status,
            "collapsed": engine.collapsed,
            "collapse_reason": engine.collapse_reason,
        }

    def handle(self, request: IEC104Message) -> Dict[str, Any]:
        payload = request.payload
        action = payload.get("action")
        if action == "snapshot":
            return self.engine.snapshot()
        if action == "get_bus":
            return self.engine.get_bus(int(payload["bus_id"]))
        if action == "get_branch":
            return self.engine.get_branch(int(payload["from_bus"]), int(payload["to_bus"]))
        # After a grid collapse the engine is latched: reads stay available but
        # every control action (including t-04 auto-reclose) is rejected, so the
        # collapsed topology/setpoints freeze until the task ends.
        if action in ("trip_line", "close_line", "set_load") and self.engine.collapsed:
            return {"status": "rejected", "reason": "system_collapsed"}
        if action == "trip_line":
            try:
                self.engine.trip_line(int(payload["from_bus"]), int(payload["to_bus"]))
            except PowerFlowUnsolvableError:
                pass
            return self._control_reply("line tripped", self.engine)
        if action == "close_line":
            try:
                self.engine.close_line(int(payload["from_bus"]), int(payload["to_bus"]))
            except PowerFlowUnsolvableError:
                pass
            return self._control_reply("line closed", self.engine)
        if action == "set_load":
            try:
                self.engine.set_load(int(payload["bus_id"]), float(payload["pd"]), float(payload["qd"]))
            except PowerFlowUnsolvableError:
                pass
            return self._control_reply("load updated", self.engine)
        raise ValueError(f"Unknown engine action: {action!r}")

    def serve(self):
        server = IEC104Server(self.host, self.port, self.handle)
        server.start()
        while True:
            time.sleep(1)


class AreaRuntime:
    def __init__(self, area_name: str, engine_host: str, engine_port: int, host: str, port: int):
        self.area_name = area_name
        self.engine_host = engine_host
        self.engine_port = engine_port
        self.host = host
        self.port = port
        self.engine = PowerFlowEngine()
        self.controller = None

    def _dispatch_to_engine(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        client = IEC104Client(self.engine_host, self.engine_port)
        return client.send(IEC104Message("command", payload))

    def _handle_request(self, request: IEC104Message) -> Dict[str, Any]:
        payload = request.payload
        action = payload.get("action")
        if action == "snapshot":
            return {
                "area": self.area_name,
                "buses": [self.engine.get_bus(bus) for bus in AREA_LAYOUT[self.area_name]],
                "branches": [
                    branch
                    for branch in self.engine.snapshot()["branches"]
                    if branch["from_bus"] in AREA_LAYOUT[self.area_name] or branch["to_bus"] in AREA_LAYOUT[self.area_name]
                ],
                "generators": [
                    gen for gen in self.engine.snapshot()["generators"] if gen["bus"] in AREA_LAYOUT[self.area_name]
                ],
            }
        if action == "trip_line":
            return self._dispatch_to_engine(payload)
        if action == "close_line":
            return self._dispatch_to_engine(payload)
        if action == "set_load":
            return self._dispatch_to_engine(payload)
        raise ValueError(f"Unsupported area action: {action!r}")

    def serve(self):
        server = IEC104Server(self.host, self.port, self._handle_request)
        server.start()
        while True:
            time.sleep(1)


class SCADARuntime:
    def __init__(self, host: str, port: int, targets: List[str]):
        self.host = host
        self.port = port
        self.targets = targets
        self.scada = SCADAController(PowerFlowEngine())

    def _request(self, target: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        host, port = target.split(":")
        client = IEC104Client(host, int(port))
        response = client.send(IEC104Message("command", payload))
        return response

    def connect(self):
        self.scada.connect()
        for target in self.targets:
            self._request(target, {"action": "snapshot"})
        return {"status": "connected", "targets": self.targets}

    def dispatch_command(self, line: str):
        parts = line.strip().split()
        if not parts:
            return
        cmd = parts[0]
        if cmd == "connect":
            print(self.connect())
        elif cmd == "disconnect":
            print(self.scada.disconnect())
        elif cmd == "branch":
            print(self.scada.branch())
        elif cmd == "bus":
            print(self.scada.bus())
        elif cmd == "generator":
            print(self.scada.generator())
        elif cmd == "trip_line":
            if len(parts) == 3:
                fbus, tbus = int(parts[1]), int(parts[2])
                area_name = next(area for area, buses in AREA_LAYOUT.items() if fbus in buses)
                print(self.scada.trip_line(area_name, fbus, tbus))
            elif len(parts) == 4:
                area_name, fbus, tbus = parts[1], int(parts[2]), int(parts[3])
                print(self.scada.trip_line(area_name, fbus, tbus))
            else:
                print("Usage: trip_line <from_bus> <to_bus>")
        elif cmd == "close_line":
            if len(parts) == 3:
                fbus, tbus = int(parts[1]), int(parts[2])
                area_name = next(area for area, buses in AREA_LAYOUT.items() if fbus in buses)
                print(self.scada.close_line(area_name, fbus, tbus))
            elif len(parts) == 4:
                area_name, fbus, tbus = parts[1], int(parts[2]), int(parts[3])
                print(self.scada.close_line(area_name, fbus, tbus))
            else:
                print("Usage: close_line <from_bus> <to_bus>")
        elif cmd == "set_load":
            if len(parts) == 5:
                area_name, bus_id, pd, qd = parts[1], int(parts[2]), float(parts[3]), float(parts[4])
                print(self.scada.set_load(area_name, bus_id, pd, qd))
            else:
                print("Usage: set_load <area> <bus_id> <pd> <qd>")
        elif cmd == "exit":
            raise SystemExit
        else:
            print("Unknown command.")

    def command_loop(self):
        print("SCADA controller ready. Type help or exit.")
        while True:
            try:
                line = input("scada> ")
                self.dispatch_command(line)
            except EOFError:
                break
            except SystemExit:
                break


def _parse_args():
    parser = argparse.ArgumentParser(description="Distributed SCADA simulation for IEC 60870-5-104 style control")
    parser.add_argument("--role", choices=["engine", "area", "scada"], required=True)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=2404)
    parser.add_argument("--engine-host", default="10.10.0.100")
    parser.add_argument("--engine-port", type=int, default=7001)
    parser.add_argument("--area-name", default="areaA")
    parser.add_argument("--area-targets", default="10.10.0.2:2404,10.10.0.3:2404,10.10.0.4:2404,10.10.0.5:2404,10.10.0.6:2404")
    return parser.parse_args()


def main():
    args = _parse_args()
    if args.role == "engine":
        EngineService(args.host, args.port).serve()
    elif args.role == "area":
        AreaRuntime(args.area_name, args.engine_host, args.engine_port, args.host, args.port).serve()
    elif args.role == "scada":
        targets = [item.strip() for item in args.area_targets.split(",") if item.strip()]
        SCADARuntime(args.host, args.port, targets).command_loop()
    else:
        raise ValueError(f"Unsupported role: {args.role}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Shutting down.")
        sys.exit(0)
