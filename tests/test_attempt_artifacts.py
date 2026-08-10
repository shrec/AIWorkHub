"""Tests for aiworkhub.attempt_artifacts."""
from __future__ import annotations
import json
import pytest
from aiworkhub.attempt_artifacts import (
    _ABSENT_SHA256_SENTINEL, _EMPTY_FILE_SHA256, ArtifactEntry,
    AttemptArtifactManifest, InvalidArtifactError, InvalidManifestError,
    parse_manifest_json, persist_json_bundle, validate_artifact_path,
    verify_json_bundle,
)

VALID_SHA256 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

def _make_entry(**overrides) -> ArtifactEntry:
    d = {"path":"src/foo.py","sha256":VALID_SHA256,"byte_count":100,"media_type":"text/plain","role":"diff","present":True,"required":True}
    d.update(overrides)
    return ArtifactEntry(**d)

def _make_entry_dict(**overrides) -> dict:
    d = {"path":"src/foo.py","sha256":VALID_SHA256,"byte_count":100,"media_type":"text/plain","role":"diff","present":True,"required":True}
    d.update(overrides)
    return d

def _make_manifest_json(**overrides) -> str:
    d = {"attempt_id":"attempt-001","artifacts":[_make_entry_dict()]}
    d.update(overrides)
    return json.dumps(d)

def test_construct_valid_entry() -> None:
    e = _make_entry()
    assert e.path == "src/foo.py"

def test_present_empty_file_with_real_digest() -> None:
    e = _make_entry(sha256=_EMPTY_FILE_SHA256, byte_count=0)
    assert e.present is True and e.byte_count == 0

def test_absent_artifact_with_sentinel() -> None:
    e = _make_entry(sha256=_ABSENT_SHA256_SENTINEL, byte_count=0, present=False)
    assert e.present is False and e.sha256 == _ABSENT_SHA256_SENTINEL

def test_manifest_sorting() -> None:
    e1 = _make_entry(path="c.py"); e2 = _make_entry(path="a.py"); e3 = _make_entry(path="b.py")
    m = AttemptArtifactManifest("id", [e1, e2, e3])
    assert [a.path for a in m.artifacts] == ["a.py","b.py","c.py"]

def test_manifest_to_json_deterministic() -> None:
    e1 = _make_entry(path="z.py"); e2 = _make_entry(path="a.py")
    m1 = AttemptArtifactManifest("id", [e1, e2])
    m2 = AttemptArtifactManifest("id", [e2, e1])
    assert m1.to_json() == m2.to_json()

@pytest.mark.parametrize("char",["\x00","\x01","\x02","\x03","\x04","\x05","\x06","\x07","\x08","\t","\n","\x0b","\x0c","\r","\x0e","\x0f","\x10","\x11","\x12","\x13","\x14","\x15","\x16","\x17","\x18","\x19","\x1a","\x1b","\x1c","\x1d","\x1e","\x1f","\x7f"])
def test_path_rejects_c0_and_del(char: str) -> None:
    path = f"src/{char}foo.py"
    with pytest.raises(InvalidArtifactError, match="control"):
        _make_entry(path=path)

def test_path_rejects_control_via_json_parse() -> None:
    s = _make_manifest_json(artifacts=[_make_entry_dict(path="src/\x00foo.py")])
    with pytest.raises((InvalidArtifactError, InvalidManifestError)):
        parse_manifest_json(s)

def test_sentinel_rejected_when_present_true() -> None:
    with pytest.raises(InvalidArtifactError, match="sentinel"):
        _make_entry(sha256=_ABSENT_SHA256_SENTINEL, byte_count=0, present=True)

def test_empty_present_requires_real_empty_digest() -> None:
    with pytest.raises(InvalidArtifactError, match="empty bytes"):
        _make_entry(sha256=VALID_SHA256, byte_count=0, present=True)

def test_present_false_requires_sentinel() -> None:
    with pytest.raises(InvalidArtifactError, match="sentinel"):
        _make_entry(sha256=VALID_SHA256, byte_count=0, present=False)

def test_present_false_requires_byte_count_zero() -> None:
    with pytest.raises(InvalidArtifactError):
        _make_entry(sha256=_ABSENT_SHA256_SENTINEL, byte_count=1, present=False)

def test_non_empty_with_empty_digest_rejected() -> None:
    with pytest.raises(InvalidArtifactError, match="non-empty"):
        _make_entry(sha256=_EMPTY_FILE_SHA256, byte_count=100, present=True)

def test_parse_rejects_duplicate_top_level_keys() -> None:
    s = '{"attempt_id":"x","attempt_id":"y","artifacts":[]}'
    with pytest.raises(InvalidManifestError, match="duplicate"):
        parse_manifest_json(s)

def test_parse_rejects_duplicate_artifact_keys() -> None:
    s = '{"attempt_id":"x","artifacts":[{"path":"a.py","path":"b.py","sha256":"'+VALID_SHA256+'","byte_count":0,"media_type":"t","role":"diff"}]}'
    with pytest.raises(InvalidManifestError, match="duplicate"):
        parse_manifest_json(s)

def test_parse_rejects_duplicate_keys_in_nested() -> None:
    s = '{"attempt_id":"x","artifacts":[{"path":"a.py","sha256":"'+VALID_SHA256+'","byte_count":0,"byte_count":1,"media_type":"t","role":"diff"}]}'
    with pytest.raises(InvalidManifestError, match="duplicate"):
        parse_manifest_json(s)

@pytest.mark.parametrize("path",["../secret.txt","src/../../etc/passwd","..","/etc/passwd","\\windows\\system32","C:\\foo.txt"])
def test_rejects_unsafe_paths(path: str) -> None:
    with pytest.raises(InvalidArtifactError):
        _make_entry(path=path)

def test_rejects_empty_path() -> None:
    with pytest.raises(InvalidArtifactError, match="non-empty"):
        _make_entry(path="")

def test_rejects_float_byte_count() -> None:
    with pytest.raises(InvalidArtifactError, match="integer"):
        ArtifactEntry(path="a.py",sha256=VALID_SHA256,byte_count=1.5,media_type="t",role="diff")

def test_rejects_bool_byte_count() -> None:
    with pytest.raises(InvalidArtifactError, match="integer"):
        ArtifactEntry(path="a.py",sha256=VALID_SHA256,byte_count=True,media_type="t",role="diff")

def test_rejects_negative_byte_count() -> None:
    with pytest.raises(InvalidArtifactError):
        _make_entry(byte_count=-1)

def test_rejects_non_bool_present() -> None:
    with pytest.raises(InvalidArtifactError, match="boolean"):
        ArtifactEntry(path="a.py",sha256=VALID_SHA256,byte_count=0,media_type="t",role="diff",present="yes")

def test_rejects_invalid_sha256() -> None:
    with pytest.raises(InvalidArtifactError, match="hex"):
        _make_entry(sha256="AAA")

def test_rejects_invalid_role() -> None:
    with pytest.raises(InvalidArtifactError, match="role"):
        _make_entry(role="not-a-role")

def test_rejects_unknown_top_level_field() -> None:
    p = json.loads(_make_manifest_json()); p["extra"] = "v"
    with pytest.raises(InvalidManifestError, match="unknown"):
        parse_manifest_json(json.dumps(p))

def test_rejects_unknown_artifact_field() -> None:
    p = json.loads(_make_manifest_json()); p["artifacts"][0]["extra"] = "v"
    with pytest.raises(InvalidManifestError, match="unknown"):
        parse_manifest_json(json.dumps(p))

def test_rejects_non_object_artifact() -> None:
    with pytest.raises(InvalidManifestError):
        parse_manifest_json('{"attempt_id":"x","artifacts":["nope"]}')

def test_rejects_duplicate_paths() -> None:
    e1 = _make_entry(path="same.py"); e2 = _make_entry(path="same.py")
    with pytest.raises(InvalidManifestError, match="duplicate"):
        AttemptArtifactManifest("id", [e1, e2])

def test_rejects_required_but_absent() -> None:
    e = _make_entry(sha256=_ABSENT_SHA256_SENTINEL, byte_count=0, present=False, required=True)
    with pytest.raises(InvalidManifestError, match="required"):
        AttemptArtifactManifest("id", [e])

def test_empty_manifest_valid() -> None:
    m = AttemptArtifactManifest("id", [])
    assert m.artifacts == []

def test_roundtrip() -> None:
    m1 = parse_manifest_json(_make_manifest_json())
    m2 = parse_manifest_json(m1.to_json())
    assert m2.to_dict() == m1.to_dict()

def test_validate_artifact_path_helper() -> None:
    assert validate_artifact_path("src/foo.py") is True
    with pytest.raises(InvalidArtifactError):
        validate_artifact_path("../etc/passwd")

def test_parse_rejects_null_attempt_id() -> None:
    with pytest.raises(InvalidManifestError):
        parse_manifest_json('{"attempt_id":null,"artifacts":[]}')

def test_parse_rejects_null_path() -> None:
    d = _make_entry_dict(); d["path"] = None
    with pytest.raises((InvalidManifestError, InvalidArtifactError)):
        parse_manifest_json(json.dumps({"attempt_id":"x","artifacts":[d]}))

def test_parse_rejects_string_byte_count() -> None:
    d = _make_entry_dict(); d["byte_count"] = "100"
    with pytest.raises(InvalidManifestError, match="integer"):
        parse_manifest_json(json.dumps({"attempt_id":"x","artifacts":[d]}))

def test_parse_defaults_present_required() -> None:
    d = _make_entry_dict(); del d["present"]; del d["required"]
    m = parse_manifest_json(json.dumps({"attempt_id":"x","artifacts":[d]}))
    a = m.artifacts[0]
    assert a.present is True and a.required is True

def test_control_in_media_type_rejected() -> None:
    with pytest.raises(InvalidArtifactError, match="control"):
        _make_entry(media_type="text/\x00plain")

def test_control_in_attempt_id_rejected() -> None:
    with pytest.raises(InvalidManifestError, match="control"):
        AttemptArtifactManifest("bad\x00id", [])

def test_malformed_json_rejected() -> None:
    with pytest.raises(InvalidManifestError):
        parse_manifest_json("not json")

def test_to_json_no_indent_single_line() -> None:
    m = AttemptArtifactManifest("id", [_make_entry()])
    assert "\n" not in m.to_json()

def test_manifest_with_multiple_artifacts_ordered() -> None:
    e1 = _make_entry(path="z.py"); e2 = _make_entry(path="a.py"); e3 = _make_entry(path="m.py")
    m = AttemptArtifactManifest("multi", [e1, e2, e3])
    assert [a.path for a in m.artifacts] == ["a.py","m.py","z.py"]


def _bundle_payloads() -> dict[str, dict[str, object]]:
    return {
        "metadata": {"request_id": "request-1"},
        "diff": {"changed_paths": ["src/example.py"]},
        "validation": {"checks": [], "passed": True},
        "usage": {"usage_observed": False},
        "review": {"target_state": "review_ready"},
    }


def test_persist_and_verify_json_bundle_roundtrip(tmp_path) -> None:
    bundle_dir = tmp_path / "attempt-1"

    receipt = persist_json_bundle(
        bundle_dir,
        attempt_id="attempt-1",
        payloads=_bundle_payloads(),
    )

    assert receipt["verified"] is True
    assert receipt["artifact_count"] == 5
    assert receipt["roles"] == ["diff", "metadata", "review", "usage", "validation"]
    assert verify_json_bundle(bundle_dir)["attempt_id"] == "attempt-1"


def test_verify_json_bundle_rejects_tampering(tmp_path) -> None:
    bundle_dir = tmp_path / "attempt-1"
    persist_json_bundle(
        bundle_dir,
        attempt_id="attempt-1",
        payloads=_bundle_payloads(),
    )
    (bundle_dir / "usage.json").write_text('{"usage_observed":true}\n', encoding="utf-8")

    with pytest.raises(InvalidArtifactError, match="mismatch"):
        verify_json_bundle(bundle_dir)


def test_persist_json_bundle_requires_core_roles(tmp_path) -> None:
    payloads = _bundle_payloads()
    payloads.pop("review")

    with pytest.raises(InvalidManifestError, match="missing required"):
        persist_json_bundle(
            tmp_path / "attempt-1",
            attempt_id="attempt-1",
            payloads=payloads,
        )


def test_verify_json_bundle_rejects_symlinked_artifact(tmp_path) -> None:
    bundle_dir = tmp_path / "attempt-1"
    persist_json_bundle(
        bundle_dir,
        attempt_id="attempt-1",
        payloads=_bundle_payloads(),
    )
    usage_path = bundle_dir / "usage.json"
    target = tmp_path / "outside.json"
    target.write_text("{}\n", encoding="utf-8")
    usage_path.unlink()
    try:
        usage_path.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")

    with pytest.raises(InvalidArtifactError, match="symlink"):
        verify_json_bundle(bundle_dir)

def test_to_dict_keys_sorted() -> None:
    d = _make_entry().to_dict()
    assert list(d.keys()) == sorted(d.keys())

def test_all_allowed_roles() -> None:
    from aiworkhub.attempt_artifacts import _ALLOWED_ROLES
    for role in sorted(_ALLOWED_ROLES):
        assert _make_entry(role=role).role == role

def test_sentinel_rejected_when_present_true_nonzero() -> None:
    with pytest.raises(InvalidArtifactError, match="sentinel"):
        _make_entry(sha256=_ABSENT_SHA256_SENTINEL, byte_count=100, present=True)

def test_very_long_path_rejected() -> None:
    with pytest.raises(InvalidArtifactError):
        _make_entry(path="a"*5000)

def test_rejects_non_dict_top_level() -> None:
    with pytest.raises(InvalidManifestError):
        parse_manifest_json('["nope"]')

def test_rejects_non_list_artifacts() -> None:
    with pytest.raises(InvalidManifestError):
        parse_manifest_json('{"attempt_id":"x","artifacts":"nope"}')

def test_rejects_non_string_manifest_input() -> None:
    with pytest.raises(InvalidManifestError):
        parse_manifest_json(123)

def test_rejects_null_present() -> None:
    d = _make_entry_dict(); d["present"] = None
    with pytest.raises(InvalidManifestError, match="boolean"):
        parse_manifest_json(json.dumps({"attempt_id":"x","artifacts":[d]}))

def test_path_rejects_c1_controls() -> None:
    for c in range(0x80, 0xA0):
        with pytest.raises(InvalidArtifactError, match="control"):
            _make_entry(path=f"src/{chr(c)}foo.py")

def test_path_rejects_c1_and_del_via_json_parse() -> None:
    for c in (0, 0x7f, 0x80, 0x9f):
        ch = chr(c)
        s = _make_manifest_json(artifacts=[_make_entry_dict(path=f"src/{ch}foo.py")])
        with pytest.raises((InvalidArtifactError, InvalidManifestError)):
            parse_manifest_json(s)

def test_attempt_id_rejects_c1_controls() -> None:
    for c in range(0x80, 0xA0):
        with pytest.raises(InvalidManifestError, match="control"):
            AttemptArtifactManifest(f"id{chr(c)}", [])

def test_attempt_id_rejects_unsafe_identifiers() -> None:
    for bad in ("a/b", "a" + chr(92) + "b", ".", ".."):
        with pytest.raises(InvalidManifestError):
            AttemptArtifactManifest(bad, [])

def test_attempt_id_rejects_unsafe_via_json_parse() -> None:
    for bad in ("a/b", "..", "id" + chr(0x80)):
        payload = json.dumps({"attempt_id":bad,"artifacts":[]})
        with pytest.raises(InvalidManifestError):
            parse_manifest_json(payload)

def test_attempt_id_allows_opaque_forms() -> None:
    for valid in ("attempt-001", "a..b", "foo_bar.baz-2", "with space"):
        m = AttemptArtifactManifest(valid, [])
        assert m.attempt_id == valid

def test_rejects_missing_path() -> None:
    d = _make_entry_dict(); del d["path"]
    with pytest.raises((InvalidManifestError, InvalidArtifactError)):
        parse_manifest_json(json.dumps({"attempt_id":"x","artifacts":[d]}))
def test_media_type_rejects_c1_controls() -> None:
    for c in range(0x80, 0xA0):
        with pytest.raises(InvalidArtifactError, match="control"):
            _make_entry(media_type=f"text/{chr(c)}plain")
def test_media_type_rejects_leading_whitespace() -> None:
    with pytest.raises(InvalidArtifactError, match="whitespace"):
        _make_entry(media_type=" text/plain")
def test_media_type_rejects_trailing_whitespace() -> None:
    with pytest.raises(InvalidArtifactError, match="whitespace"):
        _make_entry(media_type="text/plain ")
def test_media_type_whitespace_rejected_via_json_parse() -> None:
    d = _make_entry_dict(); d["media_type"] = " text/plain"
    with pytest.raises((InvalidManifestError, InvalidArtifactError), match="whitespace"):
        parse_manifest_json(json.dumps({"attempt_id":"x","artifacts":[d]}))
def test_media_type_c1_rejected_via_json_parse() -> None:
    d = _make_entry_dict(); d["media_type"] = f"text/{chr(0x80)}plain"
    with pytest.raises((InvalidManifestError, InvalidArtifactError)):
        parse_manifest_json(json.dumps({"attempt_id":"x","artifacts":[d]}))
def test_path_rejects_drive_letter_anywhere() -> None:
    for path in ("C:/bar", "C:" + chr(92) + "bar", "foo/C:/bar", "foo/C:" + chr(92) + "bar", "x/C:bar", "a/C:y/z"):
        with pytest.raises(InvalidArtifactError, match="drive"):
            _make_entry(path=path)
def test_path_drive_letter_anywhere_via_json_parse() -> None:
    d = _make_entry_dict(); d["path"] = "foo/C:/bar"
    with pytest.raises((InvalidManifestError, InvalidArtifactError)):
        parse_manifest_json(json.dumps({"attempt_id":"x","artifacts":[d]}))
