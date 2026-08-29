from pathlib import Path


# Literal descending ratchet: future decomposition work may lower, never raise it.
MAX_AIWORKHUB_MODULE_LINES = 14675


def test_aiworkhub_python_modules_stay_below_size_ratchet():
    source_dir = Path(__file__).parents[1] / 'src' / 'aiworkhub'
    oversized = {
        path.relative_to(source_dir).as_posix(): len(
            path.read_text(encoding='utf-8').splitlines()
        )
        for path in sorted(source_dir.rglob('*.py'))
        if len(path.read_text(encoding='utf-8').splitlines())
        > MAX_AIWORKHUB_MODULE_LINES
    }

    assert oversized == {}, (
        f'AIWorkHub modules exceed the {MAX_AIWORKHUB_MODULE_LINES}-line ratchet: '
        f'{oversized}'
    )
