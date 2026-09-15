from __future__ import annotations

import hashlib

import pytest

from aiworkhub import output_spill_store


def test_exact_round_trip_recovers_original_bytes(tmp_path) -> None:
    text = "line one\nline two\nunicode: café \U0001f600\n" * 50
    receipt = output_spill_store.spill_text(text, repo=tmp_path)

    assert receipt.locator.startswith("aiworkhub-spill-sha256:")
    assert str(tmp_path) not in receipt.locator
    assert receipt.original_bytes == len(text.encode("utf-8"))
    assert receipt.content_sha256 == hashlib.sha256(text.encode("utf-8")).hexdigest()

    recovered = output_spill_store.retrieve_text(receipt.locator, repo=tmp_path)
    assert recovered == text


def test_spilling_identical_text_twice_is_idempotent(tmp_path) -> None:
    text = "repeated payload" * 100
    first = output_spill_store.spill_text(text, repo=tmp_path)
    second = output_spill_store.spill_text(text, repo=tmp_path)

    assert first.locator == second.locator
    assert first.content_sha256 == second.content_sha256


def test_tampered_stored_bytes_are_refused_on_retrieval(tmp_path) -> None:
    receipt = output_spill_store.spill_text("original content", repo=tmp_path)
    stored = tmp_path / ".aiworkhub" / "spill" / f"{receipt.content_sha256}.txt"
    stored.write_bytes(b"tampered content")

    with pytest.raises(output_spill_store.OutputSpillError, match="digest_mismatch"):
        output_spill_store.retrieve_text(receipt.locator, repo=tmp_path)


def test_malformed_locator_is_refused_without_touching_disk(tmp_path) -> None:
    with pytest.raises(output_spill_store.OutputSpillError, match="locator_malformed"):
        output_spill_store.retrieve_text("not-a-real-locator", repo=tmp_path)
    with pytest.raises(output_spill_store.OutputSpillError, match="locator_malformed"):
        output_spill_store.retrieve_text(
            "aiworkhub-spill-sha256:not-hex", repo=tmp_path
        )


def test_missing_spill_file_is_refused(tmp_path) -> None:
    fake_digest = "0" * 64
    with pytest.raises(output_spill_store.OutputSpillError, match="missing"):
        output_spill_store.retrieve_text(
            f"aiworkhub-spill-sha256:{fake_digest}", repo=tmp_path
        )


def test_durability_failure_raises_fail_closed_and_leaves_no_partial_file(
    tmp_path, monkeypatch
) -> None:
    def _boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated durability failure")

    monkeypatch.setattr(output_spill_store.os, "replace", _boom)

    with pytest.raises(
        output_spill_store.OutputSpillError, match="persist_failed"
    ):
        output_spill_store.spill_text("data that must not be half-written", repo=tmp_path)

    spill_dir = tmp_path / ".aiworkhub" / "spill"
    leftovers = list(spill_dir.iterdir()) if spill_dir.exists() else []
    assert leftovers == []


def test_below_threshold_prune_is_byte_for_byte_unchanged() -> None:
    text = "small enough to keep as-is"
    result = output_spill_store.prune_text(text, max_bytes=4096)

    assert result.pruned is False
    assert result.text == text
    assert result.presented_bytes == result.original_bytes
    assert result.pruned_bytes == 0


def test_over_budget_prune_produces_bounded_head_marker_tail() -> None:
    text = "HEAD" * 500 + "MIDDLE" * 500 + "TAIL" * 500
    result = output_spill_store.prune_text(
        text, max_bytes=600, locator="aiworkhub-spill-sha256:" + "a" * 64
    )

    assert result.pruned is True
    assert result.presented_bytes <= 600
    assert len(result.text.encode("utf-8")) == result.presented_bytes
    assert result.original_bytes == len(text.encode("utf-8"))
    assert result.pruned_bytes > 0
    assert "measured_pruning_marker" in result.text
    assert "omitted_bytes=" in result.text
    assert "aiworkhub-spill-sha256:" + "a" * 64 in result.text
    assert result.text.startswith("HEAD")
    assert result.text.endswith("TAIL")


def test_prune_without_locator_marks_result_unspilled() -> None:
    result = output_spill_store.prune_text("x" * 2000, max_bytes=200)

    assert result.pruned is True
    assert "locator=unspilled" in result.text
    assert result.presented_bytes <= 200


@pytest.mark.parametrize("max_bytes", [128, 257, 512, 1000])
def test_prune_head_and_tail_stay_within_cap_across_budgets(max_bytes) -> None:
    text = "0123456789" * 1000
    result = output_spill_store.prune_text(text, max_bytes=max_bytes)

    assert result.presented_bytes <= max_bytes
    assert len(result.text.encode("utf-8")) <= max_bytes


def test_prune_budget_too_small_for_marker_is_refused() -> None:
    with pytest.raises(
        output_spill_store.OutputSpillError, match="prune_budget_too_small"
    ):
        output_spill_store.prune_text("x" * 1000, max_bytes=8)


def test_prune_respects_utf8_character_boundaries_at_the_cut_point() -> None:
    # Four-byte emoji characters placed exactly around where a naive
    # byte-slice would land mid-codepoint.
    text = "a" * 297 + "\U0001f600" * 50 + "b" * 297
    for max_bytes in range(120, 260, 7):
        result = output_spill_store.prune_text(text, max_bytes=max_bytes)
        # A safe cut never raises and always round-trips cleanly.
        assert result.text.encode("utf-8").decode("utf-8") == result.text
        assert result.presented_bytes <= max_bytes


def test_spill_and_prune_below_threshold_spills_nothing() -> None:
    text = "tiny"
    result = output_spill_store.spill_and_prune(text, repo="/does/not/matter", max_bytes=4096)

    assert result.pruned is False
    assert result.text == text
    assert result.receipt is None
    assert result.telemetry["spilled_bytes"] == 0
    assert result.telemetry["provider_token_savings"] == "UNKNOWN"


def test_spill_and_prune_over_threshold_spills_full_text_and_prunes_preview(
    tmp_path,
) -> None:
    text = "ABCDEFGHIJ" * 5000
    result = output_spill_store.spill_and_prune(text, repo=tmp_path, max_bytes=300)

    assert result.pruned is True
    assert result.receipt is not None
    assert len(result.text.encode("utf-8")) <= 300

    recovered = output_spill_store.retrieve_text(result.receipt.locator, repo=tmp_path)
    assert recovered == text
    assert result.telemetry["original_bytes"] == len(text.encode("utf-8"))
    assert result.telemetry["spilled_bytes"] == len(text.encode("utf-8"))
    assert result.telemetry["pruned_bytes"] > 0
    assert result.telemetry["provider_token_savings"] == "UNKNOWN"


def test_spill_and_prune_durability_failure_never_returns_preview_only(
    tmp_path, monkeypatch
) -> None:
    def _boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated durability failure")

    monkeypatch.setattr(output_spill_store.os, "replace", _boom)

    with pytest.raises(output_spill_store.OutputSpillError):
        output_spill_store.spill_and_prune(
            "x" * 5000, repo=tmp_path, max_bytes=300
        )


def test_spill_verifies_existing_target_by_digest_not_size_before_reuse(
    tmp_path,
) -> None:
    text = "original content for digest check"
    receipt = output_spill_store.spill_text(text, repo=tmp_path)
    stored = tmp_path / ".aiworkhub" / "spill" / f"{receipt.content_sha256}.txt"
    # Same byte length as the original, but different bytes: a size-only
    # idempotency check would wrongly treat this as the same content.
    stored.write_bytes(b"x" * len(text.encode("utf-8")))

    with pytest.raises(output_spill_store.OutputSpillError, match="collision"):
        output_spill_store.spill_text(text, repo=tmp_path)


def test_spill_fsyncs_containing_directory_after_publish(tmp_path, monkeypatch) -> None:
    real_open = output_spill_store.os.open
    real_fsync = output_spill_store.os.fsync
    opened_paths: dict[int, str] = {}
    fsynced_paths: list[str] = []

    def tracking_open(path, *args, **kwargs):
        fd = real_open(path, *args, **kwargs)
        opened_paths[fd] = str(path)
        return fd

    def tracking_fsync(fd):
        path = opened_paths.get(fd)
        if path is not None:
            fsynced_paths.append(path)
        return real_fsync(fd)

    monkeypatch.setattr(output_spill_store.os, "open", tracking_open)
    monkeypatch.setattr(output_spill_store.os, "fsync", tracking_fsync)

    output_spill_store.spill_text("durable directory entry", repo=tmp_path)

    assert str(tmp_path / ".aiworkhub" / "spill") in fsynced_paths


def test_directory_fsync_is_skipped_on_windows_without_touching_disk(
    tmp_path, monkeypatch
) -> None:
    # ``Path(...)`` itself re-checks ``os.name`` on every construction (to
    # pick Windows/Posix flavour), so the target ``Path`` must be built
    # BEFORE the patch below -- constructing one afterwards would blow up
    # with "cannot instantiate 'WindowsPath' on your system" for reasons
    # unrelated to the directory-fsync skip this test actually covers.
    target_dir = tmp_path / ".aiworkhub" / "spill"
    target_dir.mkdir(parents=True)

    def guarded_open(path, flags, *args, **kwargs):
        if flags == output_spill_store.os.O_RDONLY:
            raise AssertionError(
                "POSIX directory-fsync open must be skipped on Windows"
            )
        raise AssertionError("no other os.open call is expected in this test")

    monkeypatch.setattr(output_spill_store.os, "name", "nt")
    monkeypatch.setattr(output_spill_store.os, "open", guarded_open)

    output_spill_store._fsync_dir(target_dir)


def test_spill_and_prune_rejects_non_positive_budget_before_any_write(
    tmp_path,
) -> None:
    spill_dir = tmp_path / ".aiworkhub" / "spill"

    with pytest.raises(
        output_spill_store.OutputSpillError, match="prune_budget_non_positive"
    ):
        output_spill_store.spill_and_prune("x" * 5000, repo=tmp_path, max_bytes=0)

    assert not spill_dir.exists()

    with pytest.raises(
        output_spill_store.OutputSpillError, match="prune_budget_non_positive"
    ):
        output_spill_store.spill_and_prune("x" * 5000, repo=tmp_path, max_bytes=-1)

    assert not spill_dir.exists()
