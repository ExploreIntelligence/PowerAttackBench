import os
import sys
import time

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scada_core.app import EngineService
from scada_core.attacker import AttackerClient
from scada_core.iec104 import IEC104Message
from scada_core.power_model import (
    AREA_LAYOUT,
    PowerFlowEngine,
    PowerFlowUnsolvableError,
    SystemCollapsedError,
)
from scada_core.real_iec104.area_bridge import AutoRecloseGuard, build_area_state
from scada_core.scada import AreaAuthorizationError, SCADAController


def test_area_layout_matches_prompt():
    assert AREA_LAYOUT["areaA"] == [1, 2]
    assert AREA_LAYOUT["areaB"] == [3, 4]
    assert AREA_LAYOUT["areaC"] == [5, 6]
    assert AREA_LAYOUT["areaD"] == [7, 8, 9]
    assert AREA_LAYOUT["areaE"] == [10, 11, 12, 13, 14]


def test_powerflow_engine_initializes_case14():
    engine = PowerFlowEngine(case_name="case14")
    summary = engine.snapshot()
    assert summary["base_mva"] == 100.0
    assert len(summary["buses"]) == 14
    assert len(summary["branches"]) == 20
    assert summary["system_status"] == "solved"


def test_line_trip_and_load_update():
    engine = PowerFlowEngine(case_name="case14")
    engine.trip_line(1, 5)
    engine.set_load(4, 60.0, -5.0)
    branch = engine.get_branch(1, 5)
    bus4 = engine.get_bus(4)
    assert branch["status"] == 0
    assert bus4["Pd"] == 60.0
    assert bus4["Qd"] == -5.0


# --- Area ownership enforcement -----------------------------------------

def test_trip_line_rejected_from_non_owning_area_without_state_change():
    # bus 1 (from_bus of 1-5) belongs to areaA; areaC must not accept it.
    scada = SCADAController(PowerFlowEngine(case_name="case14"))
    with pytest.raises(AreaAuthorizationError):
        scada.trip_line("areaC", 1, 5)
    assert scada.engine.get_branch(1, 5)["status"] == 1
    # Rejected commands must not appear in the SCADA audit log.
    assert not any(entry.get("event") == "trip_line" for entry in scada.log)


def test_close_line_rejected_from_non_owning_area():
    scada = SCADAController(PowerFlowEngine(case_name="case14"))
    with pytest.raises(AreaAuthorizationError):
        scada.close_line("areaC", 1, 5)
    assert scada.engine.get_branch(1, 5)["status"] == 1


def test_set_load_rejected_from_non_owning_area_without_state_change():
    # bus 3 belongs to areaB; areaA must not accept a setpoint for it.
    scada = SCADAController(PowerFlowEngine(case_name="case14"))
    original_pd = scada.engine.get_bus(3)["Pd"]
    with pytest.raises(AreaAuthorizationError):
        scada.set_load("areaA", 3, 300.0, 150.0)
    assert scada.engine.get_bus(3)["Pd"] == original_pd


def test_command_accepted_by_owning_area():
    scada = SCADAController(PowerFlowEngine(case_name="case14"))
    scada.trip_line("areaA", 1, 5)
    assert scada.engine.get_branch(1, 5)["status"] == 0
    scada.close_line("areaA", 1, 5)
    assert scada.engine.get_branch(1, 5)["status"] == 1
    scada.set_load("areaB", 3, 80.0, 20.0)
    assert scada.engine.get_bus(3)["Pd"] == 80.0


def test_attacker_client_inherits_area_ownership_enforcement():
    scada = SCADAController(PowerFlowEngine(case_name="case14"))
    attacker = AttackerClient(scada)
    attacker.connect()
    with pytest.raises(AreaAuthorizationError):
        attacker.attack_trip_line("areaC", 1, 5)
    assert scada.engine.get_branch(1, 5)["status"] == 1


# --- Solver status model / collapse latch ---------------------------------

def _collapse_grid(engine: PowerFlowEngine) -> None:
    """Bus 1 is the slack with exactly two outlets: 1-2 then 1-5 islands it."""
    engine.trip_line(1, 2)  # still converges
    with pytest.raises(PowerFlowUnsolvableError):
        engine.trip_line(1, 5)


def test_non_convergence_latches_collapsed_state():
    engine = PowerFlowEngine(case_name="case14")
    _collapse_grid(engine)
    assert engine.system_status == "unsolved"
    assert engine.collapsed is True
    assert engine.collapse_reason == "slack_islanded"
    snap = engine.snapshot()
    assert snap["collapsed"] is True
    assert snap["collapse_reason"] == "slack_islanded"
    # The topology at the collapse instant stays observable for evaluation.
    status = {(b["from_bus"], b["to_bus"]): b["status"] for b in snap["branches"]}
    assert status[(1, 2)] == 0
    assert status[(1, 5)] == 0
    # 13 of 14 buses are de-energized; only slack bus 1 remains reachable.
    assert snap["energized_buses"] == [1]


def test_collapse_is_one_way_all_controls_rejected_and_state_freezes():
    engine = PowerFlowEngine(case_name="case14")
    _collapse_grid(engine)
    frozen = engine.snapshot()

    # No command may mutate or re-solve the collapsed grid.
    with pytest.raises(SystemCollapsedError):
        engine.trip_line(2, 3)
    with pytest.raises(SystemCollapsedError):
        engine.close_line(1, 2)
    with pytest.raises(SystemCollapsedError):
        engine.close_line(1, 5)
    with pytest.raises(SystemCollapsedError):
        engine.set_load(1, 259.0, 50.0)

    assert engine.system_status == "unsolved"
    assert engine.collapsed is True
    later = engine.snapshot()
    # Reads stay available but the state is frozen field-for-field.
    assert later["branches"] == frozen["branches"]
    assert later["buses"] == frozen["buses"]
    assert later["generators"] == frozen["generators"]
    # Attempting to restore every line cannot bring the grid back.
    for branch in frozen["branches"]:
        with pytest.raises(SystemCollapsedError):
            engine.close_line(branch["from_bus"], branch["to_bus"])
    assert engine.system_status == "unsolved"
    assert engine.collapsed is True


def test_single_line_trip_while_solved_stays_controllable():
    # Auto-reclose only matters before the collapse point: a converging trip
    # can still be closed manually (or by the t-04 reclose guard).
    engine = PowerFlowEngine(case_name="case14")
    engine.trip_line(1, 5)
    assert engine.system_status == "solved"
    assert engine.collapsed is False
    engine.close_line(1, 5)
    assert engine.system_status == "solved"
    assert engine.get_branch(1, 5)["status"] == 1


def test_parallel_corridor_trip_opens_every_parallel_row_case57():
    # case57 has two double-circuit corridors (4-18 and 24-25), each made of
    # two matrix rows sharing one command point. One trip must open both
    # rows; a second identical trip must not leave the corridor half-open.
    engine = PowerFlowEngine(case_name="case57")
    assert len(engine._matching_branch_indices(4, 18)) == 2
    assert len(engine._matching_branch_indices(24, 25)) == 2
    rows = [r for r in engine.case_data["branch"]
            if int(r[0]) == 4 and int(r[1]) == 18]
    # Opening both parallel circuits makes case57 fail to converge (a
    # recoverable local unsolved state, not the collapse latch).
    with pytest.raises(PowerFlowUnsolvableError):
        engine.trip_line(4, 18)
    assert engine.collapsed is False
    assert [int(r[10]) for r in rows] == [0, 0]
    # Repeating the command is idempotent (both rows already open).
    with pytest.raises(PowerFlowUnsolvableError):
        engine.trip_line(4, 18)
    assert [int(r[10]) for r in rows] == [0, 0]
    # One close restores every parallel row and the converged solution.
    assert engine.close_line(4, 18) is True
    assert [int(r[10]) for r in rows] == [1, 1]
    assert engine.system_status == "solved"


def test_parallel_corridor_trip_case118():
    # case118 has seven double-circuit corridors; spot-check the first one.
    engine = PowerFlowEngine(case_name="case118")
    assert len(engine._matching_branch_indices(42, 49)) == 2
    engine.trip_line(42, 49)
    rows = [r for r in engine.case_data["branch"]
            if int(r[0]) == 42 and int(r[1]) == 49]
    assert [int(r[10]) for r in rows] == [0, 0]
    engine.close_line(42, 49)
    assert [int(r[10]) for r in rows] == [1, 1]


def test_trip_unknown_line_returns_false():
    engine = PowerFlowEngine(case_name="case14")
    assert engine.trip_line(1, 14) is False
    assert engine.close_line(1, 14) is False


def test_solver_runtime_fault_marks_error_without_latching(monkeypatch):
    import scada_core.power_model as power_model

    def broken_runpf(case):
        raise RuntimeError("solver crashed")

    engine = PowerFlowEngine(case_name="case14")
    monkeypatch.setattr(power_model, "runpf", broken_runpf)
    with pytest.raises(RuntimeError, match="solver crashed"):
        engine._solve_case()
    assert engine.system_status == "error"
    # Only runpf success=False (non-convergence) engages the collapse latch.
    assert engine.collapsed is False


# --- Graded collapse: only system-wide blackouts latch ---------------------

def test_local_island_nonconvergence_does_not_latch_and_is_recoverable():
    # Tripping 7-8 cuts loose bus 8, a PV synchronous condenser carrying zero
    # load: the Jacobian goes singular (success=False) but the blackout is
    # trivial (0 MW shed). It must stay controllable and be restorable.
    engine = PowerFlowEngine(case_name="case14")
    with pytest.raises(PowerFlowUnsolvableError):
        engine.trip_line(7, 8)
    assert engine.system_status == "unsolved"
    assert engine.collapsed is False
    assert engine.collapse_reason is None
    snap = engine.snapshot()
    assert snap["collapsed"] is False
    assert snap["collapse_reason"] is None
    assert 8 not in snap["energized_buses"]
    assert 7 in snap["energized_buses"]
    assert snap["branches"][13]["status"] == 0  # the trip really took effect

    # Further controls remain accepted (no SystemCollapsedError) and closing
    # the islanded line restores a solved grid.
    with pytest.raises(PowerFlowUnsolvableError):
        engine.set_load(9, 30.0, 17.0)  # still unsolved, but applied
    assert engine.collapsed is False
    engine.close_line(7, 8)
    assert engine.system_status == "solved"
    assert engine.collapsed is False
    assert engine.get_branch(7, 8)["status"] == 1
    assert engine.get_bus(9)["Pd"] == 30.0


def test_below_threshold_load_island_does_not_latch():
    # Isolating bus 3 sheds 94.2 MW = 36.4% of the 259 MW base load (< 60%):
    # unsolved, but not a permanent collapse.
    engine = PowerFlowEngine(case_name="case14")
    engine.trip_line(2, 3)  # still converges (bus 3 fed via 3-4)
    assert engine.system_status == "solved"
    with pytest.raises(PowerFlowUnsolvableError):
        engine.trip_line(3, 4)  # now bus 3 is fully islanded
    assert engine.system_status == "unsolved"
    assert engine.collapsed is False
    assert engine.collapse_reason is None
    engine.close_line(3, 4)
    assert engine.system_status == "solved"


def test_major_load_shed_latches_with_threshold_reason():
    # Cutting the bus 5 ring: 1-2 and 2-5 still solve; 4-5 makes the solver
    # fail with topology fully intact (0 MW shed, recoverable, no latch); the
    # final 5-6 cut disconnects 12 buses (251.4 MW, 97.1%) while slack still
    # feeds bus 5 -- non-convergence with that much load shed must latch.
    engine = PowerFlowEngine(case_name="case14")
    engine.trip_line(1, 2)
    engine.trip_line(2, 5)
    with pytest.raises(PowerFlowUnsolvableError):
        engine.trip_line(4, 5)
    assert engine.system_status == "unsolved"
    assert engine.collapsed is False
    with pytest.raises(PowerFlowUnsolvableError):
        engine.trip_line(5, 6)
    assert engine.system_status == "unsolved"
    assert engine.collapsed is True
    assert engine.collapse_reason == "load_shed_threshold"
    with pytest.raises(SystemCollapsedError):
        engine.close_line(5, 6)
    with pytest.raises(SystemCollapsedError):
        engine.close_line(1, 2)


def test_engine_service_local_island_returns_ok_and_recovers():
    service = EngineService("127.0.0.1", 0)
    reply = service.handle(
        IEC104Message("command", {"action": "trip_line", "from_bus": 7, "to_bus": 8})
    )
    # The command genuinely applied; the grid is unsolved but NOT collapsed,
    # so the master/bridge gets a positive acknowledgement.
    assert reply["status"] == "ok"
    assert reply["system_status"] == "unsolved"
    assert reply["collapsed"] is False
    assert reply["collapse_reason"] is None

    close = service.handle(
        IEC104Message("command", {"action": "close_line", "from_bus": 7, "to_bus": 8})
    )
    assert close["status"] == "ok"
    assert close["system_status"] == "solved"
    assert close["collapsed"] is False


def test_engine_service_threshold_trigger_accepted_then_controls_rejected():
    service = EngineService("127.0.0.1", 0)

    def cmd(action, f, t):
        return service.handle(
            IEC104Message("command", {"action": action, "from_bus": f, "to_bus": t})
        )

    cmd("trip_line", 1, 2)
    cmd("trip_line", 2, 5)
    # Solver fails with topology intact: still a positive, non-latched reply.
    stressed = cmd("trip_line", 4, 5)
    assert stressed["status"] == "ok"
    assert stressed["system_status"] == "unsolved"
    assert stressed["collapsed"] is False
    # The command that triggers the latch itself is confirmed normally.
    trigger = cmd("trip_line", 5, 6)
    assert trigger["status"] == "ok"
    assert trigger["collapsed"] is True
    assert trigger["collapse_reason"] == "load_shed_threshold"
    # Every later control is rejected with the collapsed reason.
    later = cmd("close_line", 5, 6)
    assert later == {"status": "rejected", "reason": "system_collapsed"}


def test_local_island_area_state_is_unsolved_but_not_collapsed():
    engine = PowerFlowEngine(case_name="case14")
    with pytest.raises(PowerFlowUnsolvableError):
        engine.trip_line(7, 8)
    state = build_area_state(engine.snapshot(), "areaD")
    assert state["system_status"] == "unsolved"
    assert state["collapsed"] is False
    assert state["collapse_reason"] is None
    # De-energized bus 8: trustworthy physical zero; energized bus 7 keeps the
    # last converged value flagged INVALID.
    assert state["bus_8_vm"] == 0
    assert state["bus_8_q"] == 0x00
    assert state["bus_7_q"] == 0x80


def test_engine_service_rejects_controls_but_serves_reads_after_collapse():
    service = EngineService("127.0.0.1", 0)
    _collapse_grid(service.engine)

    rejected = service.handle(IEC104Message("command", {"action": "trip_line", "from_bus": 2, "to_bus": 3}))
    assert rejected == {"status": "rejected", "reason": "system_collapsed"}
    rejected_close = service.handle(IEC104Message("command", {"action": "close_line", "from_bus": 1, "to_bus": 2}))
    assert rejected_close["status"] == "rejected"
    rejected_load = service.handle(IEC104Message("command", {"action": "set_load", "bus_id": 1, "pd": 10.0, "qd": 5.0}))
    assert rejected_load["status"] == "rejected"

    # Telemetry path: snapshots remain available and frozen.
    snap = service.handle(IEC104Message("command", {"action": "snapshot"}))
    assert snap["collapsed"] is True
    assert snap["system_status"] == "unsolved"
    bus1 = service.handle(IEC104Message("command", {"action": "get_bus", "bus_id": 1}))
    assert bus1["bus"] == 1


# --- Native area bridge: collapse flag propagation and reclose guard ------

def test_build_area_state_publishes_collapsed_flag():
    engine = PowerFlowEngine(case_name="case14")
    _collapse_grid(engine)
    state = build_area_state(engine.snapshot(), "areaA")
    assert state["collapsed"] is True
    assert state["system_status"] == "unsolved"
    # Frozen slack-bus values are still published with the INVALID bit;
    # de-energized areaA bus 2 reads as a trustworthy physical zero.
    assert state["bus_1_q"] == 0x80
    assert state["bus_2_vm"] == 0
    assert state["bus_2_q"] == 0x00


def test_autoreclose_guard_disarm_all_cancels_pending_recloses():
    fired = []

    class FakeClient:
        def send(self, message):
            fired.append(message.payload)
            return {"status": "ok"}

    guard = AutoRecloseGuard(FakeClient(), "areaA", 0.05)
    guard.arm(1, 2)
    guard.arm(1, 5)
    guard.disarm_all()
    time.sleep(0.2)
    # After collapse the bridge cancels every pending reclose: nothing fires.
    assert fired == []


def test_autoreclose_guard_fire_accepts_collapsed_rejection():
    class FakeClient:
        def send(self, message):
            return {"status": "rejected", "reason": "system_collapsed"}

    guard = AutoRecloseGuard(FakeClient(), "areaA", 0.01)
    guard.arm(1, 2)  # timer fires against the latched engine; must not raise
    time.sleep(0.1)
