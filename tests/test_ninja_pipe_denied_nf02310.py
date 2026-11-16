"""NF-2026-02310: a quoted ninja denial is not ninja's denial.

``ninja_pipe_denied`` used to report True whenever the three tokens appeared
anywhere in stdout or stderr.  A pytest run whose failing test printed the
denial as fixture data was then classed as a sandbox restriction, and the real
failed tests were hidden behind ``validation_unsupported_in_sandbox``.  Only a
line that itself begins with ``ninja: fatal:`` is ninja's denial.
"""

from aiworkhub import windows_build_env as wbe

DENIAL = "ninja: fatal: CreateNamedPipe: Access is denied"

# Shaped like a real pytest failure whose test fixture carries the denial text.
PYTEST_SHAPED_STDOUT = "\n".join(
    [
        "=================================== FAILURES ===================================",
        "__________________________ test_appcontainer_spec_fixture ______________________",
        "    def test_appcontainer_spec_fixture():",
        ">       assert captured == expected",
        "E       AssertionError: ninja: fatal: CreateNamedPipe: Access is denied",
        "E         assert 'ninja: fatal: CreateNamedPipe: Access is denied' == ''",
        "=========================== short test summary info ============================",
        "FAILED tests/test_process_launcher_appcontainer_spec.py::test_appcontainer_spec_fixture",
        "=================== 3 failed, 10 passed in 4.21s ===================",
    ]
)


def test_pytest_failure_quoting_the_denial_is_not_ninjas_denial():
    assert wbe.ninja_pipe_denied(PYTEST_SHAPED_STDOUT, "") is False


def test_pytest_failure_quoting_the_denial_in_stderr_is_not_ninjas_denial():
    assert wbe.ninja_pipe_denied("", PYTEST_SHAPED_STDOUT) is False


def test_quoted_denial_in_diff_or_quote_lines_is_not_ninjas_denial():
    for quoted in (
        f"+    '{DENIAL}'",
        f"-    '{DENIAL}'",
        f"    '{DENIAL}'",
        f'"{DENIAL}"',
        f"E       {DENIAL}",
    ):
        assert wbe.ninja_pipe_denied(quoted, "") is False, quoted


def test_genuine_ninja_stderr_is_still_the_denial():
    assert wbe.ninja_pipe_denied("", f"{DENIAL}\n") is True


def test_genuine_ninja_stderr_with_leading_whitespace_is_still_the_denial():
    assert wbe.ninja_pipe_denied("", f"   {DENIAL}\n") is True
    assert wbe.ninja_pipe_denied("", f"\t{DENIAL}") is True


def test_genuine_denial_line_among_pytest_output_is_the_denial():
    mixed = PYTEST_SHAPED_STDOUT + "\n" + DENIAL + "\n"
    assert wbe.ninja_pipe_denied(mixed, "") is True
