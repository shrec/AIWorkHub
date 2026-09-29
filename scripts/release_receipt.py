import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from aiworkhub import sdlc_deploy_proof
except ImportError:  # a bare checkout: read the ledger module from its src tree
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from aiworkhub import sdlc_deploy_proof

_DEFAULT_REPO_ROOT = Path(__file__).resolve().parents[1]

_LEDGER_RELATIVE = Path(*sdlc_deploy_proof.LEDGER_REL)
_VERSION_FILE_RELATIVE = Path("src") / "aiworkhub" / "_version.py"
_VERSION_LITERAL = re.compile(r'__version__\s*=\s*"([^"]+)"')
_GIT_TIMEOUT_SECONDS = sdlc_deploy_proof.GIT_TIMEOUT_SECONDS
_GIT_ENV_BLOCKLIST = sdlc_deploy_proof.GIT_ENV_BLOCKLIST


class ReceiptRefused(Exception):
    pass


def _git_clean_env() -> dict[str, str]:
    # The one scrub the deploy proof uses, over the one _GIT_ENV_BLOCKLIST.
    return sdlc_deploy_proof.scrubbed_git_env()


def _git_rev_parse_head(repo_root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT_SECONDS,
            env=_git_clean_env(),
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReceiptRefused(f"git rev-parse HEAD failed: {exc}") from exc
    head = result.stdout.strip()
    if result.returncode != 0 or not head:
        raise ReceiptRefused("git rev-parse HEAD failed")
    return head


def _canonical_version(repo_root: Path) -> str:
    version_file = repo_root / _VERSION_FILE_RELATIVE
    try:
        source = version_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReceiptRefused(f"cannot read {_VERSION_FILE_RELATIVE.as_posix()}: {exc}") from exc
    match = _VERSION_LITERAL.search(source)
    if match is None:
        raise ReceiptRefused(
            f"__version__ literal missing from {_VERSION_FILE_RELATIVE.as_posix()}"
        )
    return match.group(1)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative_to_repo_root(path: Path, repo_root: Path) -> str:
    resolved = path.resolve()
    root = repo_root.resolve()
    try:
        relative = resolved.relative_to(root)
    except ValueError:
        raise ReceiptRefused(f"path outside repository: {resolved.name}") from None
    return relative.as_posix()


def _resolve_ledger_path(repo_root: Path, ledger: str | None) -> Path:
    if ledger is None:
        return repo_root / _LEDGER_RELATIVE
    candidate = Path(ledger)
    return candidate if candidate.is_absolute() else repo_root / candidate


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_ledger(path: Path | str) -> list[dict[str, Any]]:
    # One parser: the SDLC deploy proof reads the same ledger through it.
    try:
        return sdlc_deploy_proof.parse_ledger(path)
    except sdlc_deploy_proof.ReleaseLedgerError as exc:
        raise ReceiptRefused(str(exc)) from exc


def latest_confirmed(path: Path | str) -> dict[str, Any] | None:
    return sdlc_deploy_proof.newest_confirmed(load_ledger(path))


def _append_line(ledger_path: Path, entry: dict[str, Any]) -> None:
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with ledger_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry))
        handle.write("\n")


def _refuse(message: str) -> int:
    sys.stderr.write(json.dumps({"error": message}) + "\n")
    return 2


def _cmd_record(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo_root).resolve()
    try:
        canonical_version = _canonical_version(repo_root)
        if args.version != canonical_version:
            raise ReceiptRefused(
                f"version {args.version!r} does not match canonical version "
                f"{canonical_version!r} in {_VERSION_FILE_RELATIVE.as_posix()}"
            )

        vsix_path = Path(args.vsix)
        if not vsix_path.is_file():
            raise ReceiptRefused(f"vsix not found: {args.vsix}")
        vsix_sha256 = _sha256_file(vsix_path)

        previous_vsix: dict[str, str] | None = None
        if args.previous_vsix is not None:
            previous_path = Path(args.previous_vsix)
            if not previous_path.is_file():
                raise ReceiptRefused(f"previous vsix not found: {args.previous_vsix}")
            previous_vsix = {
                "path": _relative_to_repo_root(previous_path, repo_root),
                "sha256": _sha256_file(previous_path),
            }

        ledger_path = _resolve_ledger_path(repo_root, args.ledger)
        for entry in load_ledger(ledger_path):
            if entry.get("kind") == "built" and entry.get("version") == args.version:
                if entry.get("vsix_sha256") == vsix_sha256:
                    return 0
                raise ReceiptRefused(
                    f"built receipt for version {args.version!r} already exists "
                    "with a different vsix_sha256"
                )

        release_commit = _git_rev_parse_head(repo_root)
    except ReceiptRefused as exc:
        return _refuse(str(exc))

    _append_line(
        ledger_path,
        {
            "kind": "built",
            "version": args.version,
            "release_commit": release_commit,
            "vsix_sha256": vsix_sha256,
            "built_at": _iso_now(),
            "previous_vsix": previous_vsix,
            "target": "vscode_local",
        },
    )
    return 0


def _cmd_confirm(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo_root).resolve()
    if args.server_version != args.version:
        return _refuse(
            f"server_version {args.server_version!r} does not match version {args.version!r}"
        )

    ledger_path = _resolve_ledger_path(repo_root, args.ledger)
    try:
        entries = load_ledger(ledger_path)
    except ReceiptRefused as exc:
        return _refuse(str(exc))
    has_built = any(
        entry.get("kind") == "built" and entry.get("version") == args.version
        for entry in entries
    )
    if not has_built:
        return _refuse(f"no built receipt exists for version {args.version!r}")

    already_installed = any(
        entry.get("kind") == "installed" and entry.get("version") == args.version
        for entry in entries
    )
    if already_installed:
        return 0

    _append_line(
        ledger_path,
        {
            "kind": "installed",
            "version": args.version,
            "installed_at": _iso_now(),
            "server_version": args.server_version,
        },
    )
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="release_receipt",
        description="Append build/install receipts to the SDLC release ledger.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    record_parser = subparsers.add_parser("record", help="Record a built VSIX release.")
    record_parser.add_argument("--version", required=True)
    record_parser.add_argument("--vsix", required=True)
    record_parser.add_argument("--previous-vsix")
    record_parser.add_argument("--ledger")
    record_parser.add_argument("--repo-root", default=str(_DEFAULT_REPO_ROOT))
    record_parser.set_defaults(handler=_cmd_record)

    confirm_parser = subparsers.add_parser("confirm", help="Confirm a release was installed.")
    confirm_parser.add_argument("--version", required=True)
    confirm_parser.add_argument("--server-version", required=True)
    confirm_parser.add_argument("--ledger")
    confirm_parser.add_argument("--repo-root", default=str(_DEFAULT_REPO_ROOT))
    confirm_parser.set_defaults(handler=_cmd_confirm)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
