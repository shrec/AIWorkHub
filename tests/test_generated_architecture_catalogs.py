from pathlib import Path

from aiworkhub.source_graph import SOURCE_GRAPH_MODES
from scripts import generate_architecture_catalogs


def test_render_source_graph_modes_uses_runtime_tuple() -> None:
    text = generate_architecture_catalogs.render_source_graph_modes()
    lines = text.splitlines()
    assert lines[0] == generate_architecture_catalogs.BANNER
    assert [line.split("`")[1] for line in lines if line[:1].isdigit()] == list(
        SOURCE_GRAPH_MODES
    )


def test_live_catalog_is_current() -> None:
    assert generate_architecture_catalogs.check_catalogs() == []


def test_regeneration_is_idempotent(tmp_path: Path) -> None:
    generate_architecture_catalogs.write_catalogs(tmp_path)
    first = (
        tmp_path / generate_architecture_catalogs.SOURCE_GRAPH_MODE_CATALOG_RELATIVE_PATH
    ).read_text(encoding="utf-8")
    generate_architecture_catalogs.write_catalogs(tmp_path)
    second = (
        tmp_path / generate_architecture_catalogs.SOURCE_GRAPH_MODE_CATALOG_RELATIVE_PATH
    ).read_text(encoding="utf-8")
    assert first == second == generate_architecture_catalogs.render_source_graph_modes()


def test_historical_31_mode_catalog_is_stale(tmp_path: Path) -> None:
    generate_architecture_catalogs.write_catalogs(tmp_path)
    path = tmp_path / generate_architecture_catalogs.SOURCE_GRAPH_MODE_CATALOG_RELATIVE_PATH
    lines = path.read_text(encoding="utf-8").splitlines()
    mode_lines = [line for line in lines if line[:1].isdigit()]
    kept_modes = mode_lines[:31]
    path.write_text("\n".join(lines[:6] + kept_modes + [""]), encoding="utf-8")
    errors = generate_architecture_catalogs.check_catalogs(tmp_path)
    assert [error.status for error in errors] == ["stale"]


def test_manual_edit_fails_catalog_check(tmp_path: Path) -> None:
    generate_architecture_catalogs.write_catalogs(tmp_path)
    path = tmp_path / generate_architecture_catalogs.SOURCE_GRAPH_MODE_CATALOG_RELATIVE_PATH
    path.write_text(
        path.read_text(encoding="utf-8") + "\nmanual edit\n",
        encoding="utf-8",
    )
    errors = generate_architecture_catalogs.check_catalogs(tmp_path)
    assert [error.status for error in errors] == ["stale"]


def test_reordered_modes_fail_closed(tmp_path: Path) -> None:
    generate_architecture_catalogs.write_catalogs(tmp_path)
    path = tmp_path / generate_architecture_catalogs.SOURCE_GRAPH_MODE_CATALOG_RELATIVE_PATH
    modes = list(SOURCE_GRAPH_MODES)
    modes[0], modes[1] = modes[1], modes[0]
    path.write_text(
        "\n".join(
            [
                generate_architecture_catalogs.BANNER,
                "",
                "# Source Graph Modes",
                "",
                "This catalog is generated from `src.aiworkhub.source_graph.SOURCE_GRAPH_MODES`.",
                "",
                *(f"{index}. `{mode}`" for index, mode in enumerate(modes, 1)),
                "",
            ]
        ),
        encoding="utf-8",
    )
    errors = generate_architecture_catalogs.check_catalogs(tmp_path)
    assert [error.status for error in errors] == ["reordered"]


def test_missing_catalog_fails_closed(tmp_path: Path) -> None:
    errors = generate_architecture_catalogs.check_catalogs(tmp_path)
    assert [error.status for error in errors] == ["missing"]


def test_extra_banner_catalog_fails_closed(tmp_path: Path) -> None:
    generate_architecture_catalogs.write_catalogs(tmp_path)
    extra = tmp_path / "docs/generated/extra.md"
    extra.write_text(
        generate_architecture_catalogs.BANNER + "\n\n# Extra\n",
        encoding="utf-8",
    )
    errors = generate_architecture_catalogs.check_catalogs(tmp_path)
    assert [error.status for error in errors] == ["extra"]


def test_unparsable_catalog_fails_closed(tmp_path: Path) -> None:
    generate_architecture_catalogs.write_catalogs(tmp_path)
    path = tmp_path / generate_architecture_catalogs.SOURCE_GRAPH_MODE_CATALOG_RELATIVE_PATH
    path.write_text("# Manual catalog\n", encoding="utf-8")
    errors = generate_architecture_catalogs.check_catalogs(tmp_path)
    assert [error.status for error in errors] == ["unparsable"]


def test_unreadable_catalog_fails_closed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    generate_architecture_catalogs.write_catalogs(tmp_path)
    path = tmp_path / generate_architecture_catalogs.SOURCE_GRAPH_MODE_CATALOG_RELATIVE_PATH
    original_read_text = Path.read_text

    def fail_read_text(self: Path, *args, **kwargs) -> str:
        if self == path:
            raise OSError("denied")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_read_text)
    errors = generate_architecture_catalogs.check_catalogs(tmp_path)
    assert [error.status for error in errors] == ["unreadable"]


def test_authenticated_outside_root_catalog_reports_stale_without_value_error(
    tmp_path: Path,
    monkeypatch,
) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text(
        generate_architecture_catalogs.BANNER + "\n\n# Source Graph Modes\n\n1. `focus`\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        generate_architecture_catalogs,
        "expected_catalogs",
        lambda root: {outside: generate_architecture_catalogs.render_source_graph_modes()},
    )

    errors = generate_architecture_catalogs.check_catalogs(tmp_path / "repo")
    assert [error.status for error in errors] == ["stale"]
    assert generate_architecture_catalogs.format_check_error(
        errors[0],
        tmp_path / "repo",
    ) == (
        f"{outside}: generated catalog stale: catalog content differs from generator output"
    )
