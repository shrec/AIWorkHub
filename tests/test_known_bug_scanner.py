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
