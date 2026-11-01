#!/usr/bin/env python3
# aiworkhub-tool id: pyrun
# aiworkhub-tool purpose: Run Python code, a module or a script with the repo src/ first on sys.path, instead of writing an ad-hoc script.
# aiworkhub-tool usage: python tools/pyrun.py -c "import aiworkhub; print(aiworkhub.__file__)"
# aiworkhub-tool param: script:path
"""Run ``-c CODE``, ``-m MODULE`` or a script with the repository ``src/`` first."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

USAGE = "usage: pyrun.py (-c CODE | -m MODULE | SCRIPT) [ARGS...]"


def main(argv: list[str]) -> int:
    if not argv or (argv[0] in ("-c", "-m") and len(argv) < 2):
        print(USAGE, file=sys.stderr)
        return 2
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    flag = argv[0]
    if flag == "-c":
        sys.path.insert(1, "")
        sys.argv = ["-c", *argv[2:]]
        exec(compile(argv[1], "<string>", "exec"), {"__name__": "__main__"})  # noqa: S102
    elif flag == "-m":
        sys.path.insert(1, "")
        sys.argv = [argv[1], *argv[2:]]
        runpy.run_module(argv[1], run_name="__main__", alter_sys=True)
    else:
        script = Path(flag).resolve()
        sys.path.insert(1, str(script.parent))
        sys.argv = [flag, *argv[1:]]
        runpy.run_path(str(script), run_name="__main__")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
