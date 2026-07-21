from __future__ import annotations

import json

from .core import health


def main() -> None:
    print(json.dumps(health(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

