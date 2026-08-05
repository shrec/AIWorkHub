from pathlib import Path

from aiworkhub import known_bug_scanner, quality_evidence


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
    assert report["evidence_summary"] == {
        "static_candidates": 1,
        "runtime_validated": 0,
        "claim_boundary": "static_pattern_not_runtime_reproduction",
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
    assert run["properties"]["claimBoundary"] == "static_pattern_not_runtime_reproduction"
