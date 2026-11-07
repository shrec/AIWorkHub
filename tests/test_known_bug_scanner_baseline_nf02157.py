"""NF-2026-02157: known_bug_patterns blocks only on findings the candidate introduces."""

from __future__ import annotations

import json
from pathlib import Path

from aiworkhub import known_bug_scanner as kbs
from aiworkhub import quality_evidence as qe

REL = "lib/keygen.c"
BASE_SOURCE = (
    "#include <string.h>\n"
    "#include <stdlib.h>\n"
    "void build(char *dst, const char *src) {\n"
    "    strcat(dst, src);\n"
    "}\n"
    "int roll(void) {\n"
    "    return rand();\n"
    "}\n"
)


def _tree(root: Path, text: str, relative: str = REL) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return root


def _rules(rows: list[dict]) -> list[str]:
    return sorted(row["rule_id"] for row in rows)


def test_unchanged_baseline_findings_are_pre_existing_not_blocking(tmp_path: Path) -> None:
    baseline = _tree(tmp_path / "base", BASE_SOURCE)
    candidate = _tree(tmp_path / "cand", BASE_SOURCE)
    report = kbs.scan_changed_paths(candidate, [REL], baseline_root=baseline)
    assert report["passed"] is True
    assert report["findings"] == []
    assert report["errors"] == 0 and report["warnings"] == 0
    assert report["pre_existing_count"] == 2
    assert _rules(report["pre_existing_findings"]) == [
        "cpp.unbounded_copy", "crypto.weak_c_rng",
    ]
    assert report["baseline_scope"] == "baseline_root"


def test_duplicated_identical_line_still_blocks_once(tmp_path: Path) -> None:
    baseline = _tree(tmp_path / "base", BASE_SOURCE)
    duplicated = BASE_SOURCE.replace(
        "    strcat(dst, src);\n", "    strcat(dst, src);\n    strcat(dst, src);\n"
    )
    candidate = _tree(tmp_path / "cand", duplicated)
    report = kbs.scan_changed_paths(candidate, [REL], baseline_root=baseline)
    assert _rules(report["findings"]) == ["cpp.unbounded_copy"]
    assert report["warnings"] == 1
    assert report["pre_existing_count"] == 2


def test_new_pattern_on_new_line_blocks(tmp_path: Path) -> None:
    baseline = _tree(tmp_path / "base", BASE_SOURCE)
    added = BASE_SOURCE + "int again(void) {\n    return rand() + 1;\n}\n"
    candidate = _tree(tmp_path / "cand", added)
    report = kbs.scan_changed_paths(candidate, [REL], baseline_root=baseline)
    assert _rules(report["findings"]) == ["crypto.weak_c_rng"]
    assert report["findings"][0]["line"] == 10
    assert report["errors"] == 1
    assert report["passed"] is False
    assert report["pre_existing_count"] == 2


def test_no_baseline_root_matches_todays_output(tmp_path: Path) -> None:
    candidate = _tree(tmp_path / "cand", BASE_SOURCE)
    default = kbs.scan_changed_paths(candidate, [REL])
    explicit = kbs.scan_changed_paths(candidate, [REL], baseline_root=None)
    assert default == explicit
    assert _rules(default["findings"]) == ["cpp.unbounded_copy", "crypto.weak_c_rng"]
    assert default["passed"] is False
    assert default["errors"] == 1 and default["warnings"] == 1
    assert default["pre_existing_findings"] == []
    assert default["pre_existing_count"] == 0
    assert default["baseline_scope"] == "none"


def test_missing_baseline_file_keeps_whole_file_blocking(tmp_path: Path) -> None:
    baseline = tmp_path / "base"
    baseline.mkdir()
    candidate = _tree(tmp_path / "cand", BASE_SOURCE)
    report = kbs.scan_changed_paths(candidate, [REL], baseline_root=baseline)
    unscoped = kbs.scan_changed_paths(candidate, [REL])
    assert report["findings"] == unscoped["findings"]
    assert report["passed"] is False
    assert report["pre_existing_count"] == 0
    assert report["baseline_scope"] == "baseline_root"


def test_same_line_reset_clears_release(tmp_path: Path) -> None:
    reset = (
        "struct Foo { int x; };\n"
        "void a(Foo *p) {\n"
        "    delete p; p = NULL;\n"
        "}\n"
        "void b(Foo *p) {\n"
        "    p->x = 1;\n"
        "    delete p;\n"
        "}\n"
    )
    _tree(tmp_path, reset, "reset.cpp")
    report = kbs.scan_changed_paths(tmp_path, ["reset.cpp"])
    rules = _rules(report["findings"])
    assert "cpp.use_after_release_candidate" not in rules
    assert "cpp.double_release_candidate" not in rules

    unreset = (
        "struct Foo { int x; };\n"
        "void a(Foo *p) {\n"
        "    delete p;\n"
        "    p->x = 1;\n"
        "}\n"
    )
    _tree(tmp_path, unreset, "unreset.cpp")
    flagged = kbs.scan_changed_paths(tmp_path, ["unreset.cpp"])
    assert "cpp.use_after_release_candidate" in _rules(flagged["findings"])


def test_completion_gate_passes_when_only_pattern_is_pre_existing(tmp_path: Path) -> None:
    baseline = _tree(tmp_path / "base", BASE_SOURCE)
    candidate = _tree(tmp_path / "cand", BASE_SOURCE)
    gate = qe.run_completion_quality_gate(
        candidate, changed_paths=[REL], baseline_root=baseline
    )
    check = next(
        row for row in gate["checks"] if row["check_id"] == "builtin:known_bug_patterns"
    )
    assert check["status"] == qe.STATUS_PASSED
    summary = json.loads(check["summary"])
    assert summary["pre_existing_count"] == 2
    assert summary["baseline_scope"] == "baseline_root"
    assert summary["errors"] == 0


def _memory_rules(tmp_path: Path, name: str, body: str) -> list[str]:
    _tree(tmp_path, "struct Foo { int x; };\nvoid f(Foo *p, Bar s) {\n" + body + "}\n", name)
    report = kbs.scan_changed_paths(tmp_path, [name])
    return [
        rule for rule in _rules(report["findings"])
        if rule in {"cpp.double_release_candidate", "cpp.use_after_release_candidate"}
    ]


def test_same_line_double_free_before_reset_is_still_flagged(tmp_path: Path) -> None:
    assert _memory_rules(tmp_path, "free2.c", "    free(p); free(p); p = NULL;\n") == [
        "cpp.double_release_candidate",
    ]


def test_same_line_double_delete_before_reset_is_still_flagged(tmp_path: Path) -> None:
    assert _memory_rules(tmp_path, "del2.cpp", "    delete p; delete p; p = nullptr;\n") == [
        "cpp.double_release_candidate",
    ]


def test_write_through_pointer_is_not_a_reset(tmp_path: Path) -> None:
    body = "    free(p); *p = 0;\n    p->x = 1;\n"
    assert _memory_rules(tmp_path, "through.c", body) == ["cpp.use_after_release_candidate"]


def test_member_store_is_not_a_reset(tmp_path: Path) -> None:
    body = "    free(p);\n    s.p = 0;\n    p->x = 1;\n"
    assert _memory_rules(tmp_path, "member.c", body) == ["cpp.use_after_release_candidate"]


def test_same_line_delete_then_reset_stays_clean(tmp_path: Path) -> None:
    body = "    delete p; p = NULL;\n    p->x = 1;\n    delete p;\n"
    assert _memory_rules(tmp_path, "clean.cpp", body) == []


def test_typed_redeclaration_after_release_is_a_reset(tmp_path: Path) -> None:
    for name, decl in (
        ("decl.c", "char *p = malloc(4);"),
        ("decl_ptr.cpp", "Foo* p = q;"),
        ("decl_auto.cpp", "auto p = q;"),
    ):
        source = (
            "void a(char *p) {\n    free(p);\n}\n"
            "void b(Foo *q) {\n    " + decl + "\n    *p = 1;\n}\n"
        )
        _tree(tmp_path, source, name)
        rules = _rules(kbs.scan_changed_paths(tmp_path, [name])["findings"])
        assert "cpp.use_after_release_candidate" not in rules, decl
