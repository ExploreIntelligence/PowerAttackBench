import pytest

from scada_core.iec104_power_bridge import IEC104PowerBridge
from scada_core.scada import AreaAuthorizationError


def test_area_telemetry_contains_area_bus_and_branch_metrics():
    bridge = IEC104PowerBridge()
    telemetry = bridge.telemetry_for_area("areaA")

    assert telemetry["area"] == "areaA"
    assert telemetry["interval_s"] == 2
    assert telemetry["buses"]
    assert telemetry["branches"]
    assert telemetry["generators"]


def test_control_payload_trips_and_closes_branch():
    bridge = IEC104PowerBridge()
    payload = {"action": "trip_line", "from_bus": 1, "to_bus": 2}
    result = bridge.apply_control("areaA", payload)

    assert result["status"] == "ok"
    assert bridge.engine.get_branch(1, 2)["status"] == 0

    close_result = bridge.apply_control("areaA", {"action": "close_line", "from_bus": 1, "to_bus": 2})
    assert close_result["status"] == "ok"
    assert bridge.engine.get_branch(1, 2)["status"] == 1


def test_control_rejected_from_non_owning_area_without_state_change():
    # 1-5 is owned by areaA (from_bus=1); sending it to areaC must be rejected.
    bridge = IEC104PowerBridge()
    with pytest.raises(AreaAuthorizationError):
        bridge.apply_control("areaC", {"action": "trip_line", "from_bus": 1, "to_bus": 5})
    assert bridge.engine.get_branch(1, 5)["status"] == 1

    with pytest.raises(AreaAuthorizationError):
        bridge.apply_control("areaC", {"action": "close_line", "from_bus": 1, "to_bus": 5})


def test_set_load_rejected_from_non_owning_area():
    bridge = IEC104PowerBridge()
    original_pd = bridge.engine.get_bus(3)["Pd"]
    with pytest.raises(AreaAuthorizationError):
        bridge.apply_control(
            "areaA", {"action": "set_load", "bus_id": 3, "pd": 300.0, "qd": 150.0}
        )
    assert bridge.engine.get_bus(3)["Pd"] == original_pd


def test_control_to_unknown_area_rejected():
    bridge = IEC104PowerBridge()
    with pytest.raises(ValueError):
        bridge.apply_control("areaZ", {"action": "trip_line", "from_bus": 1, "to_bus": 2})
