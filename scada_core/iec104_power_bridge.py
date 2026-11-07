from __future__ import annotations

from typing import Any, Dict, List

from .power_model import AREA_LAYOUT, PowerFlowEngine, area_for_bus
from .scada import AreaAuthorizationError


class IEC104PowerBridge:
    """Bridge real IEC104 area/control semantics to the PYPOWER engine."""

    def __init__(self, case_name: str | None = None):
        self.engine = PowerFlowEngine(case_name)

    def telemetry_for_area(self, area_name: str) -> Dict[str, Any]:
        area_buses = AREA_LAYOUT[area_name]
        snapshot = self.engine.snapshot()

        return {
            "area": area_name,
            "interval_s": 2,
            "buses": [self.engine.get_bus(bus_id) for bus_id in area_buses],
            "branches": [
                branch
                for branch in snapshot["branches"]
                if branch["from_bus"] in area_buses or branch["to_bus"] in area_buses
            ],
            "generators": [
                gen for gen in snapshot["generators"] if gen["bus"] in area_buses
            ],
        }

    def apply_control(self, area_name: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if area_name not in AREA_LAYOUT:
            raise ValueError(f"Unknown area {area_name!r}")
        action = payload.get("action")

        if action == "trip_line":
            from_bus = int(payload["from_bus"])
            self._authorize(area_name, from_bus, action)
            status = self.engine.trip_line(from_bus, int(payload["to_bus"]))
            return {"status": "ok" if status else "error", "area": area_name, "action": action}

        if action == "close_line":
            from_bus = int(payload["from_bus"])
            self._authorize(area_name, from_bus, action)
            status = self.engine.close_line(from_bus, int(payload["to_bus"]))
            return {"status": "ok" if status else "error", "area": area_name, "action": action}

        if action == "set_load":
            bus_id = int(payload["bus_id"])
            self._authorize(area_name, bus_id, action)
            bus = self.engine.set_load(bus_id, float(payload["pd"]), float(payload["qd"]))
            return {"status": "ok", "area": area_name, "action": action, "bus": bus}

        raise ValueError(f"Unsupported IEC104 control action: {action!r}")

    @staticmethod
    def _authorize(area_name: str, owner_bus: int, action: str):
        if area_for_bus(owner_bus) != area_name:
            raise AreaAuthorizationError(
                f"Area {area_name!r} does not own bus {owner_bus} "
                f"(owner: {area_for_bus(owner_bus)}); {action} rejected"
            )

    def all_telemetry(self) -> Dict[str, Dict[str, Any]]:
        return {area_name: self.telemetry_for_area(area_name) for area_name in AREA_LAYOUT}
