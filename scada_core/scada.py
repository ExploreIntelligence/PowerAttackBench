from __future__ import annotations

from typing import Any, Dict, List

from .power_model import AREA_LAYOUT, PowerFlowEngine, area_for_bus


class AreaAuthorizationError(ValueError):
    """A command was sent to an area that does not own the target equipment.

    Mirrors the real IEC-104 server: trip/close is owned by the from_bus area,
    load setpoints are owned by the target bus area. Rejected commands have no
    effect and are not written to the command log.
    """


class AreaController:
    """Area controller service that owns a subset of buses and exposes telemetry to the SCADA center."""

    def __init__(self, area_name: str, engine: PowerFlowEngine):
        self.area_name = area_name
        self.buses = AREA_LAYOUT[area_name]
        self.engine = engine
        self.connected = False

    def connect(self):
        self.connected = True
        return {"area": self.area_name, "status": "connected"}

    def disconnect(self):
        self.connected = False
        return {"area": self.area_name, "status": "disconnected"}

    def telemetry(self):
        snapshot = self.engine.snapshot()
        return {
            "area": self.area_name,
            "buses": [self.engine.get_bus(bus_id) for bus_id in self.buses],
            "branches": [
                branch
                for branch in snapshot["branches"]
                if branch["from_bus"] in self.buses or branch["to_bus"] in self.buses
            ],
            "generators": [
                gen
                for gen in snapshot["generators"]
                if gen["bus"] in self.buses
            ],
        }

    def _authorize(self, owner_bus: int, action: str):
        if owner_bus not in self.buses:
            raise AreaAuthorizationError(
                f"Area {self.area_name!r} does not own bus {owner_bus} "
                f"(owner: {area_for_bus(owner_bus)}); {action} rejected"
            )

    def apply_command(self, command: dict):
        action = command.get("action")
        if action == "trip_line":
            self._authorize(int(command["from_bus"]), action)
            return self.engine.trip_line(command["from_bus"], command["to_bus"])
        if action == "close_line":
            self._authorize(int(command["from_bus"]), action)
            return self.engine.close_line(command["from_bus"], command["to_bus"])
        if action == "set_load":
            self._authorize(int(command["bus_id"]), action)
            return self.engine.set_load(command["bus_id"], command["pd"], command["qd"])
        raise ValueError(f"Unsupported action {action!r}")


class SCADAController:
    """Central SCADA controller that uses a local summary engine and connects to all area controllers."""

    def __init__(self, engine: PowerFlowEngine):
        self.engine = engine
        self.areas = {name: AreaController(name, engine) for name in AREA_LAYOUT}
        self.connected = False
        self.log: List[Dict[str, Any]] = []

    def connect(self):
        self.connected = True
        for area in self.areas.values():
            area.connect()
        self.log.append({"event": "connect", "areas": sorted(self.areas)})
        return self.status()

    def disconnect(self):
        self.connected = False
        for area in self.areas.values():
            area.disconnect()
        self.log.append({"event": "disconnect", "areas": sorted(self.areas)})
        return self.status()

    def status(self):
        return {
            "connected": self.connected,
            "areas": {name: area.connected for name, area in self.areas.items()},
        }

    def bus(self):
        return self.engine.snapshot()["buses"]

    def branch(self):
        return self.engine.snapshot()["branches"]

    def generator(self):
        return self.engine.snapshot()["generators"]

    def trip_line(self, area_name: str, from_bus: int, to_bus: int):
        if area_name not in self.areas:
            raise ValueError(f"Unknown area {area_name!r}")
        self.areas[area_name].apply_command({"action": "trip_line", "from_bus": from_bus, "to_bus": to_bus})
        self.log.append({"event": "trip_line", "area": area_name, "from_bus": from_bus, "to_bus": to_bus})
        return self.branch()

    def close_line(self, area_name: str, from_bus: int, to_bus: int):
        if area_name not in self.areas:
            raise ValueError(f"Unknown area {area_name!r}")
        self.areas[area_name].apply_command({"action": "close_line", "from_bus": from_bus, "to_bus": to_bus})
        self.log.append({"event": "close_line", "area": area_name, "from_bus": from_bus, "to_bus": to_bus})
        return self.branch()

    def set_load(self, area_name: str, bus_id: int, pd: float, qd: float):
        if area_name not in self.areas:
            raise ValueError(f"Unknown area {area_name!r}")
        self.areas[area_name].apply_command({"action": "set_load", "bus_id": bus_id, "pd": pd, "qd": qd})
        self.log.append({"event": "set_load", "area": area_name, "bus_id": bus_id, "pd": pd, "qd": qd})
        return self.bus()

    def telemetry(self):
        return {name: area.telemetry() for name, area in self.areas.items()}
