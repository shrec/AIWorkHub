#!/usr/bin/env python3
"""Record one real manager turn as a redacted provider-stream JSONL fixture.

Usage: python scripts/capture_manager_stream.py <backend_id> <model> <out.jsonl>

The turn runs through ``CliManagerBackend`` exactly as Manager Chat runs it.
The spawned process is only wrapped: every raw stdout line is written to the
fixture, redacted, and flushed while the backend reads it, so a crashed or
timed-out turn still leaves its lines. Fixed placeholders replace the
redaction: repository, workdir and home paths, the user name, e-mail
addresses, UUIDs and OpenCode ``ses_`` ids. Developer tool only; nothing
under ``src/aiworkhub`` imports it.
"""

from __future__ import annotations

import getpass
import re
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

import aiworkhub.manager_loop_backends as backends  # noqa: E402

_spawn_cli = backends._spawn_cli

PROMPT = (
    "Fixture capture. Think briefly first. Then: 1) run the shell command "
    "`git --version`; 2) create notes.txt containing the line alpha; "
    "3) edit notes.txt so the line reads beta; 4) reply with the single word done."
)

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_SES = re.compile(r"\bses_[A-Za-z0-9]+")
_UUID_PREFIX = "00000000-0000-4000-8000-"
_SES_PREFIX = "ses_fixture"
USAGE = "Usage: python scripts/capture_manager_stream.py <backend_id> <model> <out.jsonl>"


def default_workdir(backend_id: str) -> Path:
    """The capture's working directory: inside the repository, never the system temp."""
    return ROOT / ".aiworkhub" / "runtime" / "fixture_capture" / backend_id


def path_spellings(*roots: tuple[Path, str]) -> list[tuple[str, str]]:
    """Every textual spelling of each root mapped to its token, longest first."""
    pairs: dict[str, str] = {}
    for root, token in roots:
        posix = Path(root).as_posix().rstrip("/")
        if not posix:
            continue
        windows = posix.replace("/", "\\")
        for spelling in (str(root), posix, windows, windows.replace("\\", "\\\\")):
            if spelling:
                pairs.setdefault(spelling, token)
    return sorted(pairs.items(), key=lambda pair: len(pair[0]), reverse=True)


def redact_line(line: str, spellings: list[tuple[str, str]], ids: dict[str, str]) -> str:
    """Redact one raw line; ``ids`` keeps UUID and ``ses_`` placeholders stable per capture."""
    line = _EMAIL.sub("<email>", line)
    for spelling, token in spellings:
        if not spelling:
            continue
        pattern = r"(?<!\w)" + re.escape(spelling) + r"(?!\w)"
        line = re.sub(pattern, lambda _m, t=token: t, line, flags=re.IGNORECASE)

    def placeholder(original: str, prefix: str, template: str) -> str:
        if original not in ids:
            count = sum(1 for value in ids.values() if value.startswith(prefix))
            ids[original] = template.format(count + 1)
        return ids[original]

    line = _UUID.sub(lambda m: placeholder(m.group(0).lower(), _UUID_PREFIX, _UUID_PREFIX + "{:012d}"), line)
    return _SES.sub(lambda m: placeholder(m.group(0), _SES_PREFIX, _SES_PREFIX + "{}"), line)


def _user_names(home: Path) -> list[str]:
    """The home basename and the account name; a name shorter than two characters is not redacted."""
    try:
        account = getpass.getuser()
    except (KeyError, OSError, ImportError):
        account = ""
    return [name for name in dict.fromkeys((home.name, account)) if len(name) >= 2]


class _Tee:
    """The spawned process, with every stdout line also written (redacted) to ``sink``."""

    def __init__(self, process: Any, sink: Any, redact: Callable[[str], str]) -> None:
        self._process = process
        self.stdout = self._lines(process.stdout, sink, redact)

    @staticmethod
    def _lines(stream: Any, sink: Any, redact: Callable[[str], str]) -> Iterator[str]:
        for raw in stream or ():
            if raw.strip():
                sink.write(redact(raw.rstrip("\r\n")) + "\n")
                sink.flush()
            yield raw

    def __getattr__(self, name: str) -> Any:
        return getattr(self._process, name)


def capture(
    backend_id: str,
    model: str,
    out_path: Path,
    *,
    workdir: Path | None = None,
    prompt: str = PROMPT,
    spawn: Callable[..., Any] = backends._spawn_cli,
    **backend_options: Any,
) -> list[dict]:
    """Run one manager turn; write every raw provider line, redacted, to ``out_path``."""
    workdir = (workdir or default_workdir(backend_id)).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    home = Path.home()
    spellings = path_spellings((workdir, "<workdir>"), (ROOT, "<repo>"), (home.resolve(), "<home>"), (home, "<home>"))
    spellings += [(name, "<user>") for name in _user_names(home)]
    ids: dict[str, str] = {}
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="\n") as sink:

        def tee_spawn(*args: Any) -> Any:
            process = spawn(*args)
            return _Tee(process, sink, lambda line: redact_line(line, spellings, ids))

        backend = backends.CliManagerBackend(backend_id, model, workdir, spawn=tee_spawn, **backend_options)
        return list(backend.send(prompt))


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 3:
        print(USAGE, file=sys.stderr)
        return 2
    backend_id, model, out = args
    events = capture(backend_id, model, Path(out))
    print(f"{len(events)} events; fixture: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
