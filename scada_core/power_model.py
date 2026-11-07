from __future__ import annotations

import importlib
import sys
from collections import deque
from pathlib import Path

from .case_config import area_for_bus as _area_for_bus
from .case_config import area_layout, pypower_case_name

ROOT = Path(__file__).resolve().parents[1]
CANDIDATES = [
    ROOT / "pypower",
    Path("/app/pypower"),
]

for candidate in CANDIDATES:
    if candidate.exists() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from pypower.runpf import runpf

# Active case wiring (resolved from POWER_CASE / cases/*.json at import time).
AREA_LAYOUT = area_layout()
DEFAULT_PYPOWER_CASE = pypower_case_name()

# PYPOWER case builders supported by the vendored pypower copy.
_PYPOWER_CASES = ("case14", "case39", "case57", "case118")

# Non-convergence alone does not mean the grid has collapsed: a small
# de-energized island (e.g. a PV synchronous condenser cut loose) can make the
# Jacobian singular while almost all load stays served. The one-way collapse
# latch engages only when the blackout is system-wide:
#   * the slack bus itself is islanded (nothing remains reachable from it), or
#   * buses disconnected from the slack carry at least this fraction of the
#     active case's initial total active load.
#
# Case-independent: the load denominator is summed from the bus Pd column at
# engine startup, so the same constant applies to every selectable case
# (case14/case39/case57/case118 via POWER_CASE): 259.0 / 6254.2 / 1250.8 /
# 4242.0 MW respectively. Verified across all four: islanding a zero-load PV
# bus (8 on case14, 30 on case39, 10 on case118) leaves collapsed=False;
# boundary on case14: isolated bus3 sheds 36.4% (recoverable), island {3,4}
# sheds 54.8% (recoverable), cutting the bus5 ring sheds 97.1% (latches).
COLLAPSE_LOAD_RATIO = 0.60


def area_for_bus(bus_id: int) -> str:
    """Return the area that owns ``bus_id`` (mirrors the C server routing)."""
    return _area_for_bus(bus_id)


class PowerFlowUnsolvableError(RuntimeError):
    """The solver ran but the power flow did not converge for the current case."""


class SystemCollapsedError(RuntimeError):
    """The grid has collapsed (latched non-convergence); control is rejected.

    The collapsed state is one-way: no command may mutate or re-solve the case
    until the engine process is restarted. Reads (snapshots) stay available.
    """


def energized_buses(case_data: dict) -> list[int]:
    """Buses reachable from the reference (slack) bus over in-service branches.

    Pure topology: valid even when the power flow no longer solves, so it can
    label physically de-energized buses independently of convergence.
    """
    adjacency: dict[int, set[int]] = {}
    for row in case_data["bus"]:
        adjacency[int(row[0])] = set()
    for row in case_data["branch"]:
        if int(row[10]) != 1:
            continue
        fbus, tbus = int(row[0]), int(row[1])
        adjacency.setdefault(fbus, set()).add(tbus)
        adjacency.setdefault(tbus, set()).add(fbus)
    slack = [int(row[0]) for row in case_data["bus"] if int(row[1]) == 3]
    if not slack:
        slack = [1]
    reachable: set[int] = set()
    queue: deque[int] = deque(slack)
    while queue:
        bus = queue.popleft()
        if bus in reachable:
            continue
        reachable.add(bus)
        queue.extend(adjacency.get(bus, set()) - reachable)
    return sorted(reachable)


class PowerFlowEngine:
    """Thin wrapper around a PYPOWER power-flow case."""

    def __init__(self, case_name: str | None = None):
        self.case_name = case_name or DEFAULT_PYPOWER_CASE
        self.case_data = self._load_case(self.case_name)
        self.system_status = "solved"
        # One-way latch: set only when non-convergence coincides with a
        # system-wide blackout (slack islanded or load shed past the ratio).
        # Local unsolved islands stay controllable/recoverable. Once latched,
        # the grid stays collapsed for the rest of the task; only an engine
        # restart (fresh task stack) clears it.
        self.collapsed = False
        self.collapse_reason: str | None = None
        # Baseline demand captured from the unmodified case; used as the
        # blackout denominator so set_load attacks cannot move the threshold.
        self._initial_bus_load = {
            int(row[0]): float(row[2]) for row in self.case_data["bus"]
        }
        self._initial_total_load = sum(
            pd for pd in self._initial_bus_load.values() if pd > 0
        )
        self._solve_case()

    @staticmethod
    def _load_case(case_name: str):
        if case_name not in _PYPOWER_CASES:
            raise ValueError(
                f"Unsupported case {case_name!r}. Available: {sorted(_PYPOWER_CASES)}"
            )
        module = importlib.import_module(f"pypower.{case_name}")
        return getattr(module, case_name)()

    def _collapse_assessment(self) -> str | None:
        """Decide whether the current (unsolved) topology is a true collapse.

        Returns ``None`` for a recoverable local disturbance, or the latch
        reason (``"slack_islanded"`` / ``"load_shed_threshold"``) when the
        blackout is system-wide. Pure topology + baseline demand, so it stays
        valid while the power flow itself does not converge.
        """
        energized = set(energized_buses(self.case_data))
        slack = {
            int(row[0]) for row in self.case_data["bus"] if int(row[1]) == 3
        }
        if slack and energized == slack:
            return "slack_islanded"
        shed = sum(
            self._initial_bus_load[bus]
            for bus in set(self._initial_bus_load) - energized
        )
        if self._initial_total_load > 0 and (
            shed / self._initial_total_load
        ) >= COLLAPSE_LOAD_RATIO:
            return "load_shed_threshold"
        return None

    def _solve_case(self):
        # Keep the failure modes separate:
        #   * solver raised  -> system_status "error" (runtime fault),
        #   * solver returned success=False -> system_status "unsolved"
        #     (non-convergence). The mutated topology stays observable. The
        #     one-way collapse latch engages only for a system-wide blackout
        #     (slack islanded / load shed past COLLAPSE_LOAD_RATIO); a local
        #     unsolved island remains controllable and can be restored.
        try:
            result, success = runpf(self.case_data)
        except Exception:
            self.system_status = "error"
            raise
        if not success:
            self.system_status = "unsolved"
            reason = self._collapse_assessment()
            if reason is not None:
                self.collapsed = True
                self.collapse_reason = reason
            raise PowerFlowUnsolvableError("PYPOWER failed to solve the current case")
        self.case_data = result
        self.system_status = "solved"
        self.collapse_reason = None

    def _ensure_not_collapsed(self, action: str) -> None:
        if self.collapsed:
            raise SystemCollapsedError(
                f"{action} rejected: the power system has collapsed and is permanently latched"
            )

    def snapshot(self):
        buses = []
        for idx, row in enumerate(self.case_data["bus"], start=1):
            buses.append(
                {
                    "bus": idx,
                    "type": int(row[1]),
                    "Pd": float(row[2]),
                    "Qd": float(row[3]),
                    "Gs": float(row[4]),
                    "Bs": float(row[5]),
                    "area": int(row[6]),
                    "Vm": float(row[7]),
                    "Va": float(row[8]),
                }
            )

        branches = []
        for idx, row in enumerate(self.case_data["branch"], start=1):
            branches.append(
                {
                    "branch": idx,
                    "from_bus": int(row[0]),
                    "to_bus": int(row[1]),
                    "r": float(row[2]),
                    "x": float(row[3]),
                    "b": float(row[4]),
                    # PYPOWER branch columns (0-based): ... ratio=8, angle=9,
                    # status=10. Index 9 is the phase-shift angle, not status.
                    "status": int(row[10]),
                    "rateA": float(row[5]),
                    "rateB": float(row[6]),
                    "rateC": float(row[7]),
                }
            )

        generators = []
        for idx, row in enumerate(self.case_data["gen"], start=1):
            generators.append(
                {
                    "generator": idx,
                    "bus": int(row[0]),
                    "Pg": float(row[1]),
                    "Qg": float(row[2]),
                    "Vg": float(row[5]),
                    "status": int(row[7]),
                }
            )

        return {
            "base_mva": float(self.case_data["baseMVA"]),
            "buses": buses,
            "branches": branches,
            "generators": generators,
            "system_status": self.system_status,
            "collapsed": self.collapsed,
            "collapse_reason": self.collapse_reason,
            "energized_buses": energized_buses(self.case_data),
        }

    def get_bus(self, bus_id: int):
        row = self.case_data["bus"][bus_id - 1]
        return {
            "bus": bus_id,
            "type": int(row[1]),
            "Pd": float(row[2]),
            "Qd": float(row[3]),
            "Vm": float(row[7]),
            "Va": float(row[8]),
        }

    def get_generator(self, generator_id: int):
        row = self.case_data["gen"][generator_id - 1]
        return {
            "generator": generator_id,
            "bus": int(row[0]),
            "Pg": float(row[1]),
            "Qg": float(row[2]),
            "Vg": float(row[5]),
            "status": int(row[7]),
        }

    def get_branch(self, from_bus: int, to_bus: int):
        for branch in self.case_data["branch"]:
            if int(branch[0]) == from_bus and int(branch[1]) == to_bus:
                return {
                    "from_bus": int(branch[0]),
                    "to_bus": int(branch[1]),
                    "r": float(branch[2]),
                    "x": float(branch[3]),
                    "b": float(branch[4]),
                    "status": int(branch[10]),
                    "rateA": float(branch[5]),
                }
        raise KeyError(f"Branch {from_bus}-{to_bus} not found")

    def _matching_branch_indices(self, from_bus: int, to_bus: int) -> list[int]:
        """Matrix rows oriented from_bus->to_bus.

        Parallel (double-circuit) corridors share one (from, to) pair and
        map to a single command point, so one trip/close must drive every
        parallel row — otherwise the first row absorbs every command and
        the corridor can never be fully opened.
        """
        return [
            idx
            for idx, branch in enumerate(self.case_data["branch"])
            if int(branch[0]) == from_bus and int(branch[1]) == to_bus
        ]

    def trip_line(self, from_bus: int, to_bus: int):
        self._ensure_not_collapsed("trip_line")
        matches = self._matching_branch_indices(from_bus, to_bus)
        if not matches:
            return False
        for idx in matches:
            self.case_data["branch"][idx][10] = 0
        self._solve_case()
        return True

    def close_line(self, from_bus: int, to_bus: int):
        self._ensure_not_collapsed("close_line")
        matches = self._matching_branch_indices(from_bus, to_bus)
        if not matches:
            return False
        for idx in matches:
            self.case_data["branch"][idx][10] = 1
        self._solve_case()
        return True

    def set_load(self, bus_id: int, pd: float, qd: float):
        self._ensure_not_collapsed("set_load")
        row = self.case_data["bus"][bus_id - 1]
        row[2] = float(pd)
        row[3] = float(qd)
        self._solve_case()
        return self.get_bus(bus_id)
