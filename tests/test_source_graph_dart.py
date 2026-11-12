"""Dart structural lexical Source Graph evidence (NF-2026-02231)."""

from __future__ import annotations

from pathlib import Path

from aiworkhub import source_graph as sg
from aiworkhub import source_graph_ast as sgast
from aiworkhub import source_graph_languages as languages
from aiworkhub.repository_state import bootstrap_repository

# One Dart line per entry so every expected line number is index + 1.
DART_LINES = [
    "// Dart fixture for structural Source Graph evidence.",                       # 1
    "import 'package:flutter/material.dart' as m show Widget hide State;",        # 2
    "import 'dart:async';",                                                       # 3
    "export 'src/api.dart';",                                                     # 4
    "part 'src/part.dart';",                                                      # 5
    "",                                                                           # 6
    "// class CommentFake { void commentMethod() {} }",                           # 7
    "/* outer /* class NestedFake {} */ class StillCommentFake {} */",            # 8
    "",                                                                           # 9
    "const doc = '''",                                                            # 10
    "It's here:",                                                                 # 11
    "class TripleFake {",                                                         # 12
    "  void tripleMethod() {}",                                                   # 13
    "}",                                                                          # 14
    "import 'package:fake/fake.dart';",                                           # 15
    "''';",                                                                       # 16
    "const rawPath = r'C:\\dir\\';",                                              # 17
    'final interp = "value: ${render("""',                                        # 18
    "class InterpFake {",                                                         # 19
    "  void interpMethod() {}",                                                   # 20
    "}",                                                                          # 21
    '""")} done";',                                                               # 22
    "",                                                                           # 23
    "mixin Logging on Object {",                                                  # 24
    "  void log(String message) {",                                               # 25
    "    print('[log] $message');",                                               # 26
    "  }",                                                                        # 27
    "}",                                                                          # 28
    "",                                                                           # 29
    "abstract class Shape extends Base with Logging implements Comparable<Shape> {",  # 30
    "  final double size;",                                                       # 31
    "",                                                                           # 32
    "  Shape(this.size);",                                                        # 33
    "",                                                                           # 34
    "  Shape.unit() : size = 1 {",                                                # 35
    "    log('unit ${size}');",                                                   # 36
    "  }",                                                                        # 37
    "",                                                                           # 38
    "  factory Shape.fromJson(Map<String, dynamic> json) {",                      # 39
    "    return Square(json['size'] as double);",                                 # 40
    "  }",                                                                        # 41
    "",                                                                           # 42
    "  double get area => size * size;",                                          # 43
    "",                                                                           # 44
    "  Future<void> load({int retries = 3}) async {",                             # 45
    "    await Future.delayed(const Duration(seconds: 1));",                      # 46
    "  }",                                                                        # 47
    "}",                                                                          # 48
    "",                                                                           # 49
    "extension ShapeX on Shape {",                                                # 50
    "  bool get isBig {",                                                         # 51
    "    return size > 10;",                                                      # 52
    "  }",                                                                        # 53
    "}",                                                                          # 54
    "",                                                                           # 55
    "enum Color { red, green, blue }",                                            # 56
    "",                                                                           # 57
    "typedef Compare<T> = int Function(T a, T b);",                               # 58
    "",                                                                           # 59
    "int add(int a, int b) => a + b;",                                            # 60
    "",                                                                           # 61
    "Stream<int> countTo(int n) async* {",                                        # 62
    "  for (var i = 0; i < n; i++) {",                                            # 63
    "    yield i;",                                                               # 64
    "  }",                                                                        # 65
    "}",                                                                          # 66
]
DART_SOURCE = "\n".join(DART_LINES) + "\n"

FAKE_NAMES = {
    "CommentFake", "commentMethod", "NestedFake", "StillCommentFake",
    "TripleFake", "tripleMethod", "InterpFake", "interpMethod",
}


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "dart_repo"
    root.mkdir()
    bootstrap_repository(root, repo_name="dart_repo")
    target = root / "lib" / "shapes.dart"
    target.parent.mkdir()
    target.write_text(DART_SOURCE, encoding="utf-8")
    return root


def _extract(tmp_path: Path) -> sgast.FileExtraction:
    repo = _repo(tmp_path)
    return sgast.extract_file(
        repo, repo / "lib" / "shapes.dart", build_revision=sg.BUILD_REVISION,
    )


def test_dart_language_spec_is_semantic_lexical():
    assert languages.LANGUAGE_BY_ID["dart"].capability == "semantic_lexical"
    assert "Dart" in languages.__doc__
    assert "dart" in sgast.POLYGLOT_LEXICAL_LANGUAGES
    # Rows persisted by the old file-evidence extractor no longer match the
    # expected extractor set, so incremental builds reindex every .dart file.
    assert sgast.expected_extractor_ids(Path("lib/a.dart")) == frozenset(
        {sgast.POLYGLOT_LEXICAL_EXTRACTOR_ID}
    )


def test_dart_extractor_emits_every_declaration_with_exact_lines(tmp_path):
    result = _extract(tmp_path)

    assert result.status == "ok"
    assert result.language == "dart"
    assert all(item.extractor == sgast.POLYGLOT_LEXICAL_EXTRACTOR_ID for item in result.entities)
    spans = {
        (item.kind, item.name): (item.line_start, item.line_end)
        for item in result.entities
        if item.kind != "import"
    }
    assert spans == {
        ("module", "lib/shapes.dart"): (1, 67),
        ("class", "Logging"): (24, 28),
        ("method", "log"): (25, 27),
        ("class", "Shape"): (30, 48),
        ("method", "Shape"): (33, 33),
        ("method", "unit"): (35, 37),
        ("method", "fromJson"): (39, 41),
        ("method", "area"): (43, 43),
        ("method", "load"): (45, 47),
        ("class", "ShapeX"): (50, 54),
        ("method", "isBig"): (51, 53),
        ("enum", "Color"): (56, 56),
        ("struct", "Compare"): (58, 58),
        ("function", "add"): (60, 60),
        ("function", "countTo"): (62, 66),
    }

    imports = sorted(
        (edge.dst_name, edge.line) for edge in result.edges if edge.kind == "imports"
    )
    assert imports == [
        ("dart:async", 3),
        ("package:flutter/material.dart", 2),
        ("src/api.dart", 4),
        ("src/part.dart", 5),
    ]
    import_names = {item.name for item in result.entities if item.kind == "import"}
    assert import_names == {"material", "async", "api", "part"}

    inherits = {
        (edge.src_qualname.rsplit("::", 1)[-1], edge.dst_name)
        for edge in result.edges if edge.kind == "inherits"
    }
    assert inherits == {
        ("Shape", "Base"), ("Shape", "Logging"), ("Shape", "Comparable"),
        ("Logging", "Object"),
    }
    logging_edge = next(
        edge for edge in result.edges
        if edge.kind == "inherits" and edge.dst_name == "Logging"
    )
    assert logging_edge.dst_qualname == "lib/shapes.dart::Logging"
    shape_qualname = next(
        item.qualname for item in result.entities if (item.kind, item.name) == ("class", "Shape")
    )
    assert any(
        edge.kind == "defines" and edge.src_qualname == shape_qualname
        and edge.dst_name == "fromJson"
        and edge.dst_qualname == "lib/shapes.dart::Shape::Shape.fromJson"
        for edge in result.edges
    )
    constructor = next(item for item in result.entities if item.name == "fromJson")
    assert constructor.signature.startswith("factory Shape.fromJson(")


def test_dart_strings_and_comments_never_yield_symbols(tmp_path):
    result = _extract(tmp_path)

    names = {item.name for item in result.entities}
    assert not names & FAKE_NAMES
    assert not any("fake" in edge.dst_name for edge in result.edges if edge.kind == "imports")
    # Calls inside string literals or interpolation never become edges, and a
    # call statement inside a body is never a body-less declaration.
    assert not names & {"print", "render", "Square", "delayed"}
    assert not any(edge.dst_name == "render" for edge in result.edges)

    masked = sgast._mask_dart_non_code(DART_SOURCE)
    assert len(masked) == len(DART_SOURCE)
    assert masked.count("\n") == DART_SOURCE.count("\n")
    for fake in FAKE_NAMES:
        assert fake not in masked
    assert "class Shape" in masked


def test_dart_body_query_returns_exact_method_body(tmp_path):
    repo = _repo(tmp_path)
    report = sg.build_index(repo, incremental=True)
    assert report.files_changed == 1

    conn = sg.connect(sg.resolve_db_path(repo), read_only=True)
    try:
        file_row = conn.execute(
            "SELECT language, status FROM files WHERE file_path=?",
            ("lib/shapes.dart",),
        ).fetchone()
        assert tuple(file_row) == ("dart", "ok")
    finally:
        conn.close()

    named_constructor = "lib/shapes.dart::Shape::Shape.unit"
    for name, first, last in (
        ("load", 45, 47), ("area", 43, 43), ("unit", 35, 37), (named_constructor, 35, 37),
    ):
        payload = sg.body_query(repo, name, 16)
        matches = [m for m in payload["matches"] if m["file_path"] == "lib/shapes.dart"]
        assert len(matches) == 1, (name, payload)
        match = matches[0]
        assert (match["line_start"], match["line_end"]) == (first, last)
        assert match["source"].splitlines() == DART_LINES[first - 1:last]


def test_unterminated_commented_directive_never_swallows_real_import():
    source = "\n".join([
        "/*",
        "import 'fake.dart'",
        "*/",
        "import 'package:a/b.dart';",
        "export 'src/c.dart'",
        "    show C,",
        "        D;",
        "",
    ])
    result = sgast._extract_polyglot_lexical(
        "lib/a.dart", source, "0" * 64, sg.BUILD_REVISION, language="dart",
    )

    imports = sorted(
        (edge.dst_name, edge.line) for edge in result.edges if edge.kind == "imports"
    )
    assert imports == [("package:a/b.dart", 4), ("src/c.dart", 5)]
    assert not any("fake" in item.name for item in result.entities)
    export = next(item for item in result.entities if item.name == "c")
    assert export.signature == "export 'src/c.dart' show C, D;"
