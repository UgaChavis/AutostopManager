# AutoStop Manager Development and Quality

This document is the canonical local development, testing, and quality source.

## Environment

Supported Python: 3.11 and 3.12. Production is verified with the repository
virtual environment. Runtime and development dependencies are pinned in
`pyproject.toml`; `requirements.lock` contains the hash-locked resolved set.

Clean installation:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements.lock
.venv/bin/python -m pip install --no-deps -e .
```

Regenerate the lock only after a reviewed dependency change:

```bash
.venv/bin/pip-compile pyproject.toml --extra dev --generate-hashes \
  --strip-extras --allow-unsafe --output-file requirements.lock
```

## Required local gates

```bash
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check autostop_manager tests
.venv/bin/mypy autostop_manager
.venv/bin/python -m pytest -q
.venv/bin/coverage run -m pytest -q
.venv/bin/coverage report
.venv/bin/vulture autostop_manager --min-confidence 80
.venv/bin/pip-audit --progress-spinner off
.venv/bin/python -m compileall -q autostop_manager
```

Documentation and runtime-contract gates:

```bash
.venv/bin/python -m autostop_manager.cli knowledge-sync
.venv/bin/python -m autostop_manager.cli knowledge-audit
.venv/bin/python -m autostop_manager.cli annotations-audit
.venv/bin/python -m autostop_manager.cli skills-audit
.venv/bin/python -m autostop_manager.cli cleanup-audit
.venv/bin/python -m autostop_manager.cli system-audit
```

Tests must use temporary SQLite stores and fake provider/CRM/Gmail clients.
Production writes are not a test mechanism. A regression test must fail on the
old behavior before its fix is accepted.

## Change discipline

- Preserve public interfaces unless the migration updates every caller, test,
  catalog, and document in the same change.
- Keep one responsibility per module; extract only when it reduces coupling.
- Do not add empty exception handlers, hidden fallbacks, disabled checks,
  temporary TODOs, copied business data, or generated runtime artifacts.
- Use versioned migrations for schema changes and re-run migration/restart
  tests.
- For docs deletion, resolve references first, run `cleanup-audit`, then all
  knowledge audits.
