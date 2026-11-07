from __future__ import annotations

import re
from typing import Any, Dict

# Must match CMD_BUS_BASE in real_iec104/scada_server.c.
CMD_BUS_BASE = 128


def interpret_native_command(text: str) -> Dict[str, Any]:
    """Interpret the log line emitted by the native C server into a power control operation."""
    match = re.search(r"IOA=(\d+)\s+command=(\d+)", text)
    if match:
        ioa = int(match.group(1))
        value = int(match.group(2))
        if 100000 <= ioa < 200000:
            encoded = ioa - 100000
            return {
                "action": "trip_line",
                "from_bus": encoded // CMD_BUS_BASE,
                "to_bus": encoded % CMD_BUS_BASE,
            }
        if 200000 <= ioa < 300000:
            encoded = ioa - 200000
            return {
                "action": "close_line",
                "from_bus": encoded // CMD_BUS_BASE,
                "to_bus": encoded % CMD_BUS_BASE,
            }

    raise ValueError(f"Unsupported native command payload: {text!r}")
