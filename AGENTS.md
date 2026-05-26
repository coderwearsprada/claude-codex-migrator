# Repository Guidelines

## Project Structure & Module Organization

This repository is intentionally small and stdlib-only. The main CLI and migration logic live in `migrate.py`. User-facing usage and behavior notes live in `README.md`. Tests are in `tests/test_migrate.py` and import `migrate.py` directly from the repository root. There are no vendored assets or generated build artifacts.

## Build, Test, and Development Commands

- `python3 migrate.py --help` shows the full CLI surface and is the quickest smoke check for argument parsing.
- `python3 migrate.py --from claude --to codex --dry-run` previews a migration without writes.
- `python3 -m unittest discover -s tests` runs the complete test suite.
- `python3 tests/test_migrate.py` runs the same tests directly.

There is no package install step. Keep compatibility with Python 3.9+ unless the README and tests are updated together.

## Coding Style & Naming Conventions

Use idiomatic Python with 4-space indentation, type hints where they clarify data flow, and small helpers for repeated migration behavior. Existing code uses `snake_case` for functions and variables, `PascalCase` for dataclasses and test classes, and all-caps constants for migration tables.

Prefer `pathlib.Path` for filesystem paths and structured parsers (`json`, TOML helpers) over ad hoc string manipulation. Keep comments concise.

## Testing Guidelines

Tests use standard-library `unittest`. Add or update tests in `tests/test_migrate.py` for parser changes, round-trip metadata handling, filesystem writes, backup/restore behavior, and new migration mappings. Name methods with `test_...` and group related cases in focused `unittest.TestCase` classes.

Before submitting changes, run:

```bash
python3 -m unittest discover -s tests
```

For CLI behavior changes, also run a relevant `--dry-run` against temporary or fixture directories.

## Commit & Pull Request Guidelines

Recent commits use short, imperative subject lines, for example `Fix --restore picking the wrong backup after a multi-tool sequence` and `README: split Tier B into per-source-tool tables`. Describe the user-visible change first; add body text only when needed.

Pull requests should include a summary, affected migration paths, test commands run, and compatibility impact for Claude Code, Codex CLI, or Cursor. Link related issues when available.

## Security & Configuration Tips

Do not add tests or examples that copy real credentials, session history, or auth files. The migrator ignores secrets and caches; preserve that boundary when adding paths. Use `--dry-run` and temporary directories for local validation.
