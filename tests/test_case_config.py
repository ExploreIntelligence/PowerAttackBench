"""Case-config consistency: the `lines` point table must mirror the vendored
PYPOWER case branch matrix exactly, because the C server validates trip/close
IOAs against `lines` while the power flow executes against the branch matrix.
Any drift would let the controller positively confirm a command the engine
cannot apply (or reject one it could)."""

import importlib
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "pypower"))

from scada_core.case_config import case_lines, validate_case_config  # noqa: E402

CASES = {"ieee14": "case14", "ieee39": "case39", "ieee57": "case57", "ieee118": "case118"}


@pytest.mark.parametrize("case_name,ppc_name", sorted(CASES.items()))
def test_lines_match_pypower_branch_matrix(case_name, ppc_name):
    module = importlib.import_module(ppc_name)
    ppc = getattr(module, ppc_name)()
    ppc_pairs = [(int(row[0]), int(row[1])) for row in ppc["branch"]]

    assert case_lines(case_name) == ppc_pairs


def test_case_lines_rejects_unknown_case():
    from scada_core.case_config import case_file_path

    with pytest.raises(FileNotFoundError):
        case_file_path("ieee999")


def test_validate_case_config_requires_lines():
    config = {
        "case_name": "ieee14",
        "pypower_case": "case14",
        "cmd_bus_base": 128,
        "areas": {"areaA": [1, 2], "areaB": [3, 4], "areaC": [5, 6],
                  "areaD": [7, 8, 9], "areaE": [10, 11, 12, 13, 14]},
    }
    with pytest.raises(ValueError, match="lines"):
        validate_case_config(config)

    config["lines"] = ["1-2", "bad", "1-3"]
    with pytest.raises(ValueError, match="invalid line entry"):
        validate_case_config(config)
