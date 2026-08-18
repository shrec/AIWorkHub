from pathlib import Path

import pytest

from aiworkhub import attempt_artifacts, known_bug_scanner, quality_evidence


def test_cuda_intrinsic_mismatch_is_error(tmp_path: Path):
    (tmp_path / "kernel.cu").write_text(
        "// rotl32(8)\nuint32_t f(uint32_t x) { return __byte_perm(x, 0, 0x0321); }\n",
        encoding="utf-8",
    )
    report = known_bug_scanner.scan_changed_paths(tmp_path, ["kernel.cu"])
    assert report["passed"] is False
    assert report["findings"][0]["rule_id"] == "cuda.byte_perm_rotation_mismatch"


def test_warning_is_visible_but_nonblocking(tmp_path: Path):
    (tmp_path / "legacy.cpp").write_text(
        "void f(char *a, const char *b) { strcpy(a, b); }\n", encoding="utf-8"
    )
    report = known_bug_scanner.scan_changed_paths(tmp_path, ["legacy.cpp"])
    assert report["passed"] is True and report["warnings"] == 1


def test_comment_is_not_scanned(tmp_path: Path):
    (tmp_path / "safe.py").write_text("# subprocess.run(x, shell=True)\n", encoding="utf-8")
    assert known_bug_scanner.scan_changed_paths(tmp_path, ["safe.py"])["findings"] == []


def test_python_string_literals_are_not_scanned(tmp_path: Path):
    (tmp_path / "safe.py").write_text(
        'message = "subprocess.run(x, shell=True) and eval(user)"\n',
        encoding="utf-8",
    )
    assert known_bug_scanner.scan_changed_paths(tmp_path, ["safe.py"])["findings"] == []


def test_javascript_regexp_exec_is_not_mislabeled_as_process_shell(tmp_path: Path):
    (tmp_path / "safe.js").write_text(
        "const match = /hello/.exec(message);\n",
        encoding="utf-8",
    )
    assert known_bug_scanner.scan_changed_paths(tmp_path, ["safe.js"])["findings"] == []


def test_completion_builtin_blocks_high_confidence_pattern(tmp_path: Path):
    (tmp_path / "runner.py").write_text("subprocess.run(x, shell=True)\n", encoding="utf-8")
    checks = quality_evidence.run_builtin_static_checks(tmp_path, changed_paths=["runner.py"])
    check = next(row for row in checks if row.check_id == "builtin:known_bug_patterns")
    assert check.status == quality_evidence.STATUS_FAILED
    assert "python.shell_true" in check.summary
    assert "static_pattern_not_runtime_reproduction" in check.summary


def test_transport_security_rules_cover_multiple_languages(tmp_path: Path):
    cases = {
        "client.py": "requests.get(url, verify=False)\n",
        "client.ts": "const opts = { rejectUnauthorized: false };\n",
        "client.go": "cfg := &tls.Config{InsecureSkipVerify: true}\n",
        "client.php": "curl_setopt($ch, CURLOPT_SSL_VERIFYPEER, false);\n",
        "client.cs": "handler.ServerCertificateCustomValidationCallback = (a,b,c,d) => true;\n",
    }
    for path, source in cases.items():
        (tmp_path / path).write_text(source, encoding="utf-8")
    report = known_bug_scanner.scan_changed_paths(tmp_path, cases)
    assert report["passed"] is False
    assert report["errors"] == len(cases)
    assert {row["path"] for row in report["findings"]} == set(cases)


def test_cpp_release_sequence_is_warning_not_false_blocker(tmp_path: Path):
    (tmp_path / "lifetime.cpp").write_text(
        "void f(Item *item) {\n"
        "    delete item;\n"
        "    item->run();\n"
        "}\n",
        encoding="utf-8",
    )
    report = known_bug_scanner.scan_changed_paths(tmp_path, ["lifetime.cpp"])
    finding = next(
        row for row in report["findings"]
        if row["rule_id"] == "cpp.use_after_release_candidate"
    )
    assert finding["severity"] == "warning"
    assert report["passed"] is True


def test_cpp_release_reassignment_clears_lifetime_candidate(tmp_path: Path):
    (tmp_path / "lifetime.cpp").write_text(
        "void f(Item *item) {\n"
        "    delete item;\n"
        "    item = make_item();\n"
        "    item->run();\n"
        "}\n",
        encoding="utf-8",
    )
    report = known_bug_scanner.scan_changed_paths(tmp_path, ["lifetime.cpp"])
    assert not any(
        row["rule_id"] == "cpp.use_after_release_candidate"
        for row in report["findings"]
    )


def test_findings_preserve_static_candidate_truth_boundary(tmp_path: Path):
    (tmp_path / "client.py").write_text(
        "requests.get(url, verify=False)\n", encoding="utf-8"
    )
    report = known_bug_scanner.scan_changed_paths(tmp_path, ["client.py"])
    finding = report["findings"][0]
    assert finding["cwe"] == "CWE-295"
    assert finding["verification_state"] == "static_candidate"
    assert finding["runtime_validated"] is False
    assert len(finding["root_cause_fingerprint"]) == 64
    assert report["evidence_summary"] == {
        "static_candidates": 1,
        "runtime_validated": 0,
        "claim_boundary": "static_pattern_not_runtime_reproduction",
    }


def test_root_cause_identity_survives_unrelated_line_movement(tmp_path: Path):
    path = tmp_path / "runner.py"
    path.write_text("subprocess.run(value, shell=True)\n", encoding="utf-8")
    first = known_bug_scanner.scan_changed_paths(tmp_path, ["runner.py"])
    path.write_text(
        "unrelated = 1\nsubprocess.run(value, shell=True)\n",
        encoding="utf-8",
    )
    second = known_bug_scanner.scan_changed_paths(tmp_path, ["runner.py"])
    assert first["findings"][0]["fingerprint"] != second["findings"][0]["fingerprint"]
    assert first["findings"][0]["root_cause_fingerprint"] == (
        second["findings"][0]["root_cause_fingerprint"]
    )


def test_duplicate_static_candidates_are_counted_without_being_dropped(
    tmp_path: Path,
) -> None:
    (tmp_path / "runner.py").write_text(
        "subprocess.run(value, shell=True)\n"
        "subprocess.run(value, shell=True)\n",
        encoding="utf-8",
    )
    report = known_bug_scanner.scan_changed_paths(tmp_path, ["runner.py"])
    assert len(report["findings"]) == 2
    assert report["dedupe_summary"] == {
        "candidate_count": 2,
        "unique_root_causes": 1,
        "duplicate_candidates": 1,
        "identity_scope": "rule_path_normalized_source",
    }


def test_sarif_export_is_stable_and_does_not_upgrade_evidence(tmp_path: Path):
    (tmp_path / "runner.py").write_text(
        "subprocess.run(value, shell=True)\n", encoding="utf-8"
    )
    report = known_bug_scanner.scan_changed_paths(tmp_path, ["runner.py"])
    sarif = known_bug_scanner.to_sarif(report)
    run = sarif["runs"][0]
    result = run["results"][0]
    assert sarif["version"] == "2.1.0"
    assert result["ruleId"] == "python.shell_true"
    assert result["locations"][0]["physicalLocation"]["artifactLocation"]["uri"] == "runner.py"
    assert result["properties"]["verificationState"] == "static_candidate"
    assert result["properties"]["runtimeValidated"] is False
    assert len(result["partialFingerprints"]["aiworkhubRootCauseFingerprint"]) == 64
    assert run["properties"]["claimBoundary"] == "static_pattern_not_runtime_reproduction"


def _runtime_bundle(tmp_path: Path, *, returncode: int = 1) -> Path:
    bundle = tmp_path / "attempt-artifacts" / "request-1"
    attempt_artifacts.persist_json_bundle(
        bundle,
        attempt_id="request-1",
        payloads={
            "metadata": {"request_identity": {"request_id": "request-1"}},
            "diff": {"changed_paths": ["runner.py"]},
            "validation": {
                "checks": [{
                    "check_id": "reproduce:python.shell_true",
                    "command": ["python", "tests/reproduce_shell_true.py"],
                    "returncode": returncode,
                    "duration_seconds": 0.25,
                }],
            },
            "usage": {"usage_observed": False},
            "review": {"target_state": "review_ready"},
        },
    )
    return bundle


def test_manager_sealed_runtime_receipt_upgrades_exact_finding(tmp_path: Path):
    (tmp_path / "runner.py").write_text(
        "subprocess.run(value, shell=True)\n", encoding="utf-8"
    )
    report = known_bug_scanner.scan_changed_paths(tmp_path, ["runner.py"])
    finding = report["findings"][0]
    key = b"manager-owned-test-key"
    receipt = known_bug_scanner.issue_runtime_reproduction_receipt(
        report,
        bundle_dir=_runtime_bundle(tmp_path),
        root_cause_fingerprint=finding["root_cause_fingerprint"],
        check_id="reproduce:python.shell_true",
        expected_returncodes=[1],
        verified_by="codex-manager",
        coordinator_key=key,
    )

    bound = known_bug_scanner.bind_runtime_reproduction_receipts(
        report, [receipt], coordinator_key=key
    )

    upgraded = bound["findings"][0]
    assert upgraded["verification_state"] == "runtime_validated"
    assert upgraded["runtime_validated"] is True
    assert upgraded["runtime_reproduction_receipt"]["command"] == [
        "python", "tests/reproduce_shell_true.py"
    ]
    assert bound["evidence_summary"]["runtime_validated"] == 1
    assert bound["evidence_summary"]["unvalidated_static_candidates"] == 0
    sarif_result = known_bug_scanner.to_sarif(bound)["runs"][0]
    assert sarif_result["properties"]["runtimeValidated"] == 1
    assert sarif_result["results"][0]["properties"]["runtimeReproduction"][
        "attemptId"
    ] == "request-1"


def test_runtime_receipt_rejects_model_style_unsigned_or_tampered_claim(tmp_path: Path):
    (tmp_path / "runner.py").write_text(
        "subprocess.run(value, shell=True)\n", encoding="utf-8"
    )
    report = known_bug_scanner.scan_changed_paths(tmp_path, ["runner.py"])
    finding = report["findings"][0]
    key = b"manager-owned-test-key"
    receipt = known_bug_scanner.issue_runtime_reproduction_receipt(
        report,
        bundle_dir=_runtime_bundle(tmp_path),
        root_cause_fingerprint=finding["root_cause_fingerprint"],
        check_id="reproduce:python.shell_true",
        expected_returncodes=[1],
        verified_by="codex-manager",
        coordinator_key=key,
    )
    receipt["returncode"] = 0

    with pytest.raises(ValueError, match="signature"):
        known_bug_scanner.bind_runtime_reproduction_receipts(
            report, [receipt], coordinator_key=key
        )


def test_runtime_receipt_rejects_revision_drift(tmp_path: Path):
    path = tmp_path / "runner.py"
    path.write_text("subprocess.run(value, shell=True)\n", encoding="utf-8")
    report = known_bug_scanner.scan_changed_paths(tmp_path, ["runner.py"])
    finding = report["findings"][0]
    key = b"manager-owned-test-key"
    receipt = known_bug_scanner.issue_runtime_reproduction_receipt(
        report,
        bundle_dir=_runtime_bundle(tmp_path),
        root_cause_fingerprint=finding["root_cause_fingerprint"],
        check_id="reproduce:python.shell_true",
        expected_returncodes=[1],
        verified_by="codex-manager",
        coordinator_key=key,
    )
    path.write_text(
        "prefix = True\nsubprocess.run(value, shell=True)\n", encoding="utf-8"
    )
    changed_report = known_bug_scanner.scan_changed_paths(tmp_path, ["runner.py"])

    with pytest.raises(ValueError, match="revision mismatch"):
        known_bug_scanner.bind_runtime_reproduction_receipts(
            changed_report, [receipt], coordinator_key=key
        )


def test_runtime_receipt_requires_matching_exit_semantics(tmp_path: Path):
    (tmp_path / "runner.py").write_text(
        "subprocess.run(value, shell=True)\n", encoding="utf-8"
    )
    report = known_bug_scanner.scan_changed_paths(tmp_path, ["runner.py"])
    finding = report["findings"][0]

    with pytest.raises(ValueError, match="exit semantics"):
        known_bug_scanner.issue_runtime_reproduction_receipt(
            report,
            bundle_dir=_runtime_bundle(tmp_path, returncode=0),
            root_cause_fingerprint=finding["root_cause_fingerprint"],
            check_id="reproduce:python.shell_true",
            expected_returncodes=[1],
            verified_by="codex-manager",
            coordinator_key=b"manager-owned-test-key",
        )


def test_non_python_string_literal_is_not_a_finding(tmp_path: Path):
    # Defect 7. Before: non-Python files were only truncated at the first
    # ``//``, so ``system(cmd)`` inside a string literal became a finding.
    # After: string literals are masked, so only the real gets() call fires.
    (tmp_path / "s.c").write_text(
        'const char *msg = "system(cmd)";\n'
        "int deref(int *p){ *p = gets(buf); return 0; }\n",
        encoding="utf-8",
    )
    report = known_bug_scanner.scan_changed_paths(tmp_path, ["s.c"])
    rule_ids = {row["rule_id"] for row in report["findings"]}
    assert "cpp.process_shell" not in rule_ids
    assert "cpp.dangerous_gets" in rule_ids


def test_star_prefixed_c_line_is_scanned_not_skipped(tmp_path: Path):
    # Defect 7. Before: any line starting with ``*`` was skipped as a
    # block-comment continuation, hiding a real dereference-assignment.
    # After: masking removes real comments, so a ``*out = ...`` line is scanned.
    (tmp_path / "star.c").write_text(
        "int f(char *buf){\n*out = gets(buf);\nreturn 0;\n}\n",
        encoding="utf-8",
    )
    report = known_bug_scanner.scan_changed_paths(tmp_path, ["star.c"])
    assert any(row["rule_id"] == "cpp.dangerous_gets" for row in report["findings"])


def test_crypto_only_rule_fires_regardless_of_keyword_order(tmp_path: Path):
    # Defect 6. Before: the crypto flag accumulated line by line, so a
    # crypto_only rule fired only when the crypto keyword appeared *above* the
    # offending line. After: crypto context is a whole-file property.
    (tmp_path / "below.c").write_text(
        "int gen(void){ return rand(); }\nint key_material = 1;\n", encoding="utf-8"
    )
    (tmp_path / "above.c").write_text(
        "int key_material = 1;\nint gen(void){ return rand(); }\n", encoding="utf-8"
    )
    below = known_bug_scanner.scan_changed_paths(tmp_path, ["below.c"])
    above = known_bug_scanner.scan_changed_paths(tmp_path, ["above.c"])
    assert any(row["rule_id"] == "crypto.weak_c_rng" for row in below["findings"])
    assert any(row["rule_id"] == "crypto.weak_c_rng" for row in above["findings"])


def test_oversized_file_is_recorded_as_skipped_not_clean(tmp_path: Path):
    # Defect 8. Before: a file over MAX_FILE_BYTES returned no findings, was
    # counted in paths_considered, and could still leave passed=True. After: it
    # appears in skipped_paths with a reason and passed reflects the skip.
    oversized = tmp_path / "big.py"
    oversized.write_text("x = 1\n" * (known_bug_scanner.MAX_FILE_BYTES // 2), encoding="utf-8")
    report = known_bug_scanner.scan_changed_paths(tmp_path, ["big.py"])
    assert report["passed"] is False
    assert report["skipped_paths"] == [{
        "path": "big.py",
        "reason": "file_exceeds_max_bytes",
        "size_bytes": oversized.stat().st_size,
        "max_bytes": known_bug_scanner.MAX_FILE_BYTES,
    }]
    assert report["findings"] == []


def test_small_clean_file_has_no_skips_and_passes(tmp_path: Path):
    # The skip machinery must not regress the clean path: a small, clean file
    # is still passed with an empty skipped_paths list.
    (tmp_path / "ok.py").write_text("value = 1\n", encoding="utf-8")
    report = known_bug_scanner.scan_changed_paths(tmp_path, ["ok.py"])
    assert report["passed"] is True
    assert report["skipped_paths"] == []


def test_non_python_string_continuation_preserves_line_numbers(tmp_path: Path):
    # Rework defect 1. Before: a backslash-newline continuation inside a C
    # string literal consumed the newline too, so ``_generic_code_lines``
    # returned one fewer line than the source and every later finding drifted
    # onto the wrong line. After: the continuation newline is preserved, the
    # masked line count matches ``splitlines()`` and gets() lands on line 3.
    text = (
        'const char *msg = "part one \\\n'
        'part two";\n'
        "int f(char *buf){ return gets(buf); }\n"
    )
    masked = known_bug_scanner._masked_code_lines(text, ".c")
    assert len(masked) == len(text.splitlines())
    (tmp_path / "cont.c").write_text(text, encoding="utf-8")
    report = known_bug_scanner.scan_changed_paths(tmp_path, ["cont.c"])
    finding = next(
        row for row in report["findings"] if row["rule_id"] == "cpp.dangerous_gets"
    )
    assert finding["line"] == 3
    # The masked string content is not itself a finding.
    assert "cpp.process_shell" not in {row["rule_id"] for row in report["findings"]}


def test_source_revision_changes_with_exact_scanned_bytes(tmp_path: Path):
    path = tmp_path / "runner.py"
    path.write_text("subprocess.run(value, shell=True)\n", encoding="utf-8")
    first = known_bug_scanner.scan_changed_paths(tmp_path, ["runner.py"])
    path.write_text(
        "subprocess.run(other, shell=True)\n", encoding="utf-8"
    )
    second = known_bug_scanner.scan_changed_paths(tmp_path, ["runner.py"])
    assert first["source_revision_sha256"] != second["source_revision_sha256"]
    assert first["source_revision_scope"] == "exact_paths_and_bytes"
