from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
ACTION_MAJOR_RE = re.compile(r"uses:\s+([^\s@]+)@v(\d+)\s*$")
NODE24_MINIMUM_MAJORS = {
    "actions/checkout": 6,
    "actions/setup-python": 6,
    "actions/setup-node": 5,
    "actions/setup-go": 6,
    "actions/upload-artifact": 6,
    "actions/download-artifact": 7,
    "actions/configure-pages": 6,
    "actions/upload-pages-artifact": 5,
    "actions/deploy-pages": 5,
    "softprops/action-gh-release": 3,
}


def test_workflows_pin_node24_compatible_action_majors() -> None:
    observed: dict[str, list[tuple[Path, int]]] = {}
    for path in sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml")):
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            match = ACTION_MAJOR_RE.search(raw_line)
            if match is None:
                continue
            action, major = match.groups()
            if action in NODE24_MINIMUM_MAJORS:
                observed.setdefault(action, []).append((path, int(major)))

    assert set(observed) == set(NODE24_MINIMUM_MAJORS)
    for action, minimum in NODE24_MINIMUM_MAJORS.items():
        for path, major in observed[action]:
            assert major >= minimum, (
                f"{path.relative_to(REPO_ROOT)} uses {action}@v{major}; "
                f"Node 24 requires v{minimum} or newer"
            )
