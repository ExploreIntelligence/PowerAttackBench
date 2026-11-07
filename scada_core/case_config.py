"""Case configuration loader.

The target power system (IEEE 14/39/57/118) and the per-area bus ownership
are defined by a JSON file under the top-level ``cases/`` directory. The
active case is selected with the ``POWER_CASE`` environment variable
(default ``ieee14``). Both the host-side task manager and the in-container
Python services use this module, so the JSON files are the single source
of truth for case/area wiring.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Tuple

# scada_core/case_config.py -> project root (host and /app/power_attacker).
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_CASES_DIRS = [
    Path(os.environ.get("POWER_CASES_DIR", "")) if os.environ.get("POWER_CASES_DIR") else None,
    _PROJECT_ROOT / "cases",
    Path("/app/power_attacker/cases"),
]

AREA_ORDER = ["areaA", "areaB", "areaC", "areaD", "areaE"]
DEFAULT_CASE = "ieee14"


def cases_dir() -> Path:
    for candidate in _CASES_DIRS:
        if candidate is not None and candidate.is_dir():
            return candidate
    raise FileNotFoundError(
        "cases/ directory not found; set POWER_CASES_DIR or run from the project tree"
    )


def active_case_name() -> str:
    return os.environ.get("POWER_CASE", DEFAULT_CASE)


def case_file_path(case_name: str | None = None) -> Path:
    name = case_name or active_case_name()
    path = cases_dir() / f"{name}.json"
    if not path.is_file():
        available = ", ".join(sorted(p.stem for p in cases_dir().glob("*.json")))
        raise FileNotFoundError(f"Unknown case {name!r}. Available cases: {available}")
    return path


@lru_cache(maxsize=None)
def load_case_config(case_name: str | None = None) -> Dict[str, object]:
    path = case_file_path(case_name)
    with open(path, "r", encoding="utf-8") as handle:
        config = json.load(handle)
    validate_case_config(config)
    return config


def get_case_config() -> Dict[str, object]:
    """Configuration of the case selected by ``POWER_CASE`` (process-cached)."""
    return load_case_config(active_case_name())


def area_layout(case_name: str | None = None) -> Dict[str, List[int]]:
    areas = load_case_config(case_name)["areas"]
    return {name: list(buses) for name, buses in areas.items()}


def pypower_case_name(case_name: str | None = None) -> str:
    return str(load_case_config(case_name)["pypower_case"])


def cmd_bus_base(case_name: str | None = None) -> int:
    return int(load_case_config(case_name)["cmd_bus_base"])


def case_lines(case_name: str | None = None) -> List[Tuple[int, int]]:
    """Directed (from_bus, to_bus) branch pairs of the selected case.

    Mirrors the ``branch`` matrix of the vendored PYPOWER case exactly —
    including f > t rows and parallel branches — so the C server's point
    table and ``PowerFlowEngine.trip_line`` accept the same set of lines.
    Parallel (double-circuit) rows appear as duplicate entries; they share
    one command point, and one trip/close drives every parallel matrix row.
    """
    lines: List[Tuple[int, int]] = []
    for entry in load_case_config(case_name)["lines"]:
        left, _, right = str(entry).partition("-")
        lines.append((int(left), int(right)))
    return lines


def area_for_bus(bus_id: int, case_name: str | None = None) -> str:
    """Return the area that owns ``bus_id`` for the selected case."""
    for area_name, buses in area_layout(case_name).items():
        if bus_id in buses:
            return area_name
    raise ValueError(f"Bus {bus_id} is not assigned to any area")


def validate_case_config(config: Dict[str, object]) -> None:
    """Structural validation: 5 named areas, buses unique, positive, in range."""
    name = config.get("case_name")
    if not isinstance(name, str) or not name:
        raise ValueError("case config: 'case_name' must be a non-empty string")
    if not isinstance(config.get("pypower_case"), str):
        raise ValueError(f"case config {name}: 'pypower_case' must be a string")
    base = config.get("cmd_bus_base")
    if not isinstance(base, int) or base <= 1:
        raise ValueError(f"case config {name}: 'cmd_bus_base' must be an integer > 1")

    # Directed branch pairs ("from-to") forming the controller point table.
    # Must match the vendored PYPOWER case's branch matrix; the consistency
    # test (tests/test_case_config.py) guards against drift.
    lines = config.get("lines")
    if not isinstance(lines, list) or not lines:
        raise ValueError(f"case config {name}: 'lines' must be a non-empty list")
    for entry in lines:
        if not isinstance(entry, str):
            raise ValueError(f"case config {name}: invalid line entry {entry!r}")
        left, sep, right = entry.partition("-")
        if not sep or not left.isdigit() or not right.isdigit():
            raise ValueError(f"case config {name}: invalid line entry {entry!r}")
        if int(left) <= 0 or int(right) <= 0:
            raise ValueError(f"case config {name}: invalid line entry {entry!r}")

    areas = config.get("areas")
    if not isinstance(areas, dict) or set(areas) != set(AREA_ORDER):
        raise ValueError(
            f"case config {name}: 'areas' must contain exactly {AREA_ORDER}"
        )

    seen: Dict[int, str] = {}
    for area_name in AREA_ORDER:
        buses = areas[area_name]
        if not isinstance(buses, list) or not buses:
            raise ValueError(f"case config {name}: area {area_name} must be a non-empty list")
        for bus in buses:
            if not isinstance(bus, int) or bus <= 0:
                raise ValueError(f"case config {name}: invalid bus {bus!r} in {area_name}")
            if bus in seen:
                raise ValueError(
                    f"case config {name}: bus {bus} assigned to both "
                    f"{seen[bus]} and {area_name}"
                )
            seen[bus] = area_name

    # Contiguous 1..N coverage (all vendored PYPOWER standard cases satisfy this).
    bus_ids = sorted(seen)
    expected = list(range(1, len(bus_ids) + 1))
    if bus_ids != expected:
        missing = sorted(set(expected) - set(bus_ids))
        extra = sorted(b for b in bus_ids if b > len(bus_ids))
        raise ValueError(
            f"case config {name}: areas must cover buses 1..{len(bus_ids)} "
            f"without gaps; missing={missing} out-of-range={extra}"
        )
