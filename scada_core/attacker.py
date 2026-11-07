from __future__ import annotations

from typing import Any, Dict, List


class AttackerClient:
    """Adversary model: can reach the central SCADA controller but not the protected regional RTUs directly."""

    def __init__(self, scada):
        self.scada = scada
        self.connected = False

    def connect(self):
        self.connected = True
        self.scada.connect()
        return {"status": "connected", "target": "SCADA Center"}

    def disconnect(self):
        self.connected = False
        self.scada.disconnect()
        return {"status": "disconnected", "target": "SCADA Center"}

    def enumerate_targets(self):
        return sorted(self.scada.areas.keys())

    def attack_trip_line(self, area_name: str, from_bus: int, to_bus: int):
        if not self.connected:
            raise RuntimeError("Attacker is not connected to the SCADA center")
        return self.scada.trip_line(area_name, from_bus, to_bus)

    def attack_set_load(self, area_name: str, bus_id: int, pd: float, qd: float):
        if not self.connected:
            raise RuntimeError("Attacker is not connected to the SCADA center")
        return self.scada.set_load(area_name, bus_id, pd, qd)

    def attack_close_line(self, area_name: str, from_bus: int, to_bus: int):
        if not self.connected:
            raise RuntimeError("Attacker is not connected to the SCADA center")
        return self.scada.close_line(area_name, from_bus, to_bus)
