from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from aiworkhub import (  # noqa: E402
    core,
    manager_ai_tools,
    process_launcher,
    source_graph,
    source_graph_partition,
    task_store,
)

FINISHED = "a" * 32
READY = "b" * 32
CURRENT = "c" * 32
BROKEN = "d" * 32
UNKNOWN = "e" * 32
STRAY_PIN = ".sg-base-pin.1.2.3.4.sqlite"


def _fake_manager(cards: dict[str, dict | None], broken: tuple[str, ...] = ()):
    def request_events(request_id: str) -> list:
        if request_id in broken:
            raise RuntimeError("lookup failed")
        card = cards.get(request_id)
        return [] if card is None else [{"task_id": card["task_id"], "state": "x"}]

    def show_task(task_id: str) -> dict:
        card = next(c for c in cards.values() if c and c["task_id"] == task_id)
        return {"returncode": 0, "stdout": json.dumps(card)}

    return SimpleNamespace(
        repo=Path("."), _request_events=request_events, _show_task=show_task
    )


def _card(task_id: str, status: str, substatus: str) -> dict:
    return {
        "task_id": task_id,
        "status": status,
        "worker_status": status,
        "terminal_substatus": substatus,
    }


@pytest.fixture
def overlay_root(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "overlay"
    for name in (FINISHED, READY, CURRENT, BROKEN, UNKNOWN):
        (root / name).mkdir(parents=True)
        (root / name / STRAY_PIN).write_bytes(b"pin")
    cards = {
        FINISHED: _card("T-FIN", "done", "accepted"),
        READY: _card("T-READY", "review", "review_ready"),
        CURRENT: _card("T-CUR", "done", "accepted"),
        BROKEN: _card("T-BROKEN", "done", "accepted"),
        UNKNOWN: None,
    }
    manager = _fake_manager(cards, broken=(BROKEN,))
    monkeypatch.setattr(process_launcher, "default_manager", lambda: manager)
    return root


def test_sweep_removes_finished_and_keeps_live_current_and_unresolvable(overlay_root):
    removed = manager_ai_tools._sweep_candidate_overlays(overlay_root, CURRENT)

    assert removed == 2
    assert not (overlay_root / FINISHED).exists()
    assert not (overlay_root / UNKNOWN).exists()
    assert (overlay_root / READY).is_dir()
    assert (overlay_root / CURRENT).is_dir()
    assert (overlay_root / BROKEN).is_dir()


def test_sweep_keeps_dir_when_card_lookup_raises(overlay_root, monkeypatch):
    manager = _fake_manager({FINISHED: _card("T-FIN", "done", "accepted")})

    def explode(task_id: str):
        raise RuntimeError("card lookup failed")

    manager._show_task = explode
    monkeypatch.setattr(process_launcher, "default_manager", lambda: manager)

    manager_ai_tools._sweep_candidate_overlays(overlay_root, CURRENT)

    assert (overlay_root / FINISHED).is_dir()


def test_sweep_never_raises_without_manager_or_root(tmp_path, monkeypatch):
    def explode():
        raise RuntimeError("no manager")

    monkeypatch.setattr(process_launcher, "default_manager", explode)

    assert manager_ai_tools._sweep_candidate_overlays(tmp_path / "missing", CURRENT) == 0
    (tmp_path / "root" / FINISHED).mkdir(parents=True)
    assert manager_ai_tools._sweep_candidate_overlays(tmp_path / "root", CURRENT) == 0
    assert (tmp_path / "root" / FINISHED).is_dir()


def test_sweep_skips_symlinked_dir_and_never_follows_it(overlay_root, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    link = overlay_root / ("f" * 32)
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink privilege unavailable")

    manager_ai_tools._sweep_candidate_overlays(overlay_root, CURRENT)

    assert link.is_symlink()
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_sweep_swallows_removal_oserror(overlay_root, monkeypatch):
    import shutil

    def refuse(path, *args, **kwargs):
        raise PermissionError("sharing violation")

    monkeypatch.setattr(shutil, "rmtree", refuse)

    assert manager_ai_tools._sweep_candidate_overlays(overlay_root, CURRENT) == 0
    assert (overlay_root / FINISHED).is_dir()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_rebuild_removes_superseded_overlay_and_unreferenced_pin(
    tmp_path, monkeypatch
):
    repo = (tmp_path / "repo").resolve()
    repo.mkdir()
    assert task_store.initialize_repository(repo)["ok"]
    (repo / "src").mkdir()
    (repo / "src" / "mod.py").write_bytes(b"def canonical_nf01824():\n    return 1\n")
    source_graph.build_index(repo, incremental=False)

    base = repo / ".aiworkhub" / "runtime" / "worktrees" / CURRENT
    worktree = base / "worktree"
    home = base / "home"
    (worktree / "src").mkdir(parents=True)
    home.mkdir()
    body = "def added_nf01824():\n    return 'a'\n"
    target = worktree / "src" / "added_nf01824.py"
    target.write_bytes(body.encode("utf-8"))
    card = {
        "task_id": "T-CUR",
        "status": "review",
        "worker_status": "review",
        "terminal_substatus": "review_ready",
        "terminal_review": {
            "evidence": {
                "workspace": {
                    "request_id": CURRENT,
                    "repo": str(repo),
                    "path": str(worktree),
                    "home": str(home),
                    "allowed_writes": ["src/added_nf01824.py"],
                    "parent_baseline": {},
                },
                "changed_path_hashes": {"src/added_nf01824.py": _sha(body)},
            }
        },
    }
    fake = SimpleNamespace(
        repo=repo,
        _request_events=lambda rid: (
            [{"task_id": "T-CUR", "state": "review_ready"}] if rid == CURRENT else []
        ),
        _show_task=lambda task_id: {"returncode": 0, "stdout": json.dumps(card)},
    )
    monkeypatch.setattr(process_launcher, "default_manager", lambda: fake)
    monkeypatch.setattr(
        core,
        "manager_bootstrap",
        lambda: {
            "ok": True,
            "role": "manager",
            "provider": "codex",
            "repo": str(repo),
            "manager_route": {"provider": "codex", "session_id": "s-1", "thread_id": "s-1"},
        },
    )

    def query():
        return manager_ai_tools.source_graph_query(
            mode="bodygrep", query="added_nf01824", budget=16, request_id=CURRENT
        )

    assert query()["ok"] is True
    cache_dir = repo / manager_ai_tools._CANDIDATE_OVERLAY_DIR / CURRENT
    first = sorted(cache_dir.glob("overlay_*.sqlite"))
    assert len(first) == 1
    (cache_dir / STRAY_PIN).write_bytes(b"stale")
    finished = repo / manager_ai_tools._CANDIDATE_OVERLAY_DIR / FINISHED
    finished.mkdir()
    (finished / "overlay_x.sqlite").write_bytes(b"x")

    revised = body + "\n\ndef second_nf01824():\n    return 2\n"
    target.write_bytes(revised.encode("utf-8"))
    card["terminal_review"]["evidence"]["changed_path_hashes"][
        "src/added_nf01824.py"
    ] = _sha(revised)

    result = query()

    assert result["ok"] is True
    second = sorted(cache_dir.glob("overlay_*.sqlite"))
    assert len(second) == 1 and second != first
    assert not (cache_dir / STRAY_PIN).exists()
    pins = [
        p
        for p in cache_dir.iterdir()
        if source_graph_partition._BASE_PIN_NAME.fullmatch(p.name)
    ]
    assert len(pins) == 1
    assert not finished.exists()

    stray_after_hit = cache_dir / STRAY_PIN
    stray_after_hit.write_bytes(b"stale")
    assert query()["ok"] is True
    assert stray_after_hit.exists()
