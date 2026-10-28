"""Provider stand-in for the lifecycle canary (RM-2026-00015 criterion 3).

The launcher runs this script in place of a provider CLI, inside the isolated
worker workspace (its working directory).  It writes the canary card's one
required output and exits 0.  Extra argv the launcher appends (worker MCP
config flags) and stdin are ignored: the canary does no model work.
"""

from __future__ import annotations

import sys
from pathlib import Path

CANARY_OUTPUT = "out/canary.txt"
CANARY_TEXT = "lifecycle canary output\n"


def main() -> int:
    target = Path.cwd() / CANARY_OUTPUT
    target.parent.mkdir(parents=True, exist_ok=True)
    # Bytes, not text: the output must be identical on every platform.
    target.write_bytes(CANARY_TEXT.encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
