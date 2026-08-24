# Contributing to Discord Ferry

Thank you for your interest in contributing!

## Ways to contribute

- **Report bugs**: open an issue with the bug report template
- **Suggest features**: open an issue with the feature request template
- **Improve docs**: fix typos, add screenshots, improve guides
- **Write code**: see below

## Development setup

1. Clone the repo: `git clone https://github.com/nordscope-fi/Discord-stoat-ferry.git`
2. Install uv: `pip install uv`
3. Install dependencies: `uv sync --locked --extra dev --extra native`. This installs the test, lint, type-check, and desktop-window toolchains against the pinned `uv.lock`. The `mutation` extra (cosmic-ray) and the `docs` extra (mkdocs-material) are opt-in. Add `--extra mutation` or `--extra docs` only when you need them.
4. Run the full local suite (lint, format, types, tests): `uv run ruff check . && uv run ruff format --check . && uv run mypy src/ && uv run pytest`
5. Run the GUI in dev mode: `uv run ferry-gui`

### Cross-platform agent support (optional)

The repo ships with a generator that produces gitignored `.codex/`, `.vibe/` and `.qwen/`
directories from `config/agent-compat/`, so Codex (OpenAI CLI), Vibe (Mistral CLI) and Qwen Code
share the same lifecycle hooks and skills Claude Code uses. If you use any of them, run:

- `./scripts/agent-install.sh`: generate the per-agent configuration
- `./scripts/agent-check.sh`: verify the generated state has not drifted from the source templates

Qwen Code additionally needs `MISTRAL_API_KEY` in the `env` block of `~/.qwen/settings.json` so
the second-opinion MCP server registers its Mistral review tools. The installer prints this step.

Contributors using Claude Code do not need to run either script; the `.claude/` directory is the
source the generator renders from.

## Code style

- We use `ruff` for linting and formatting, and `mypy` for type checking. `ruff check .` targets the whole repo (source, tests, scripts, docs snippets), which is deliberate. A `src/`-only sweep misses regressions in the tests and CI helpers:
  `uv run ruff check . && uv run ruff format --check . && uv run mypy src/`
- Type hints on all public functions
- Docstrings on all public functions (Google style)

## Pull request process

1. Fork the repo and create a branch from `main`
2. Make your changes
3. Run `uv run ruff check . && uv run ruff format --check . && uv run mypy src/ && uv run pytest`
4. Open a PR with a clear description of what and why
5. Wait for review. We aim to respond within a few days

## Code of conduct

This project follows the Contributor Covenant v2.1.
Be kind, be respectful, be helpful.
