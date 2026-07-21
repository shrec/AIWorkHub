# Contributing to AIWorkHub

Thank you for your interest in AIWorkHub (AWH).

## Getting Started

1. Fork the repository and clone your fork.
2. Install the Python package:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -e .
   pip install pytest  # for tests
   ```
3. Run tests:
   ```bash
   python -m pytest tests/ -v
   ```
4. For VS Code extension changes:
   ```bash
   npm --prefix vscode-extension install
   npm --prefix vscode-extension test
   npm --prefix vscode-extension run package
   test -s vscode-extension/dist/aiworkhub-*.vsix
   ```

## Code of Conduct

This project follows a standard Code of Conduct (see `CODE_OF_CONDUCT.md`).
Be respectful, constructive, and inclusive.

## Opening Issues

- **Bug reports**: include AIWorkHub version, Python version, OS, and a
  minimal reproduction.
- **Feature requests**: describe the use case and desired behaviour.
- **Security issues**: do not open a public issue — see `SECURITY.md`.

## Pull Requests

- Keep changes focused. One PR per feature or fix.
- Update or add tests for any changed behaviour.
- Ensure `python -m pytest tests/ -v` passes before requesting review.
- Extension changes should pass `npm --prefix vscode-extension test` and
  produce `vscode-extension/dist/aiworkhub-*.vsix`.

## License

By contributing, you agree that your contributions will be licensed under
the MIT License (see `LICENSE`).
