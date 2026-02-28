# Contributing to Discord Ferry

Thank you for your interest in contributing!

## Ways to contribute

- **Report bugs** — open an issue with the bug report template
- **Suggest features** — open an issue with the feature request template
- **Improve docs** — fix typos, add screenshots, improve guides
- **Write code** — see below

## Development setup

1. Clone the repo: `git clone https://github.com/psthubhorizon/Discord-stoat-ferry.git`
2. Install uv: `pip install uv`
3. Install dependencies: `uv sync --all-extras`
4. Run tests: `uv run pytest`
5. Run the GUI in dev mode: `uv run ferry-gui`

## Code style

- We use `ruff` for linting and formatting, and `mypy` for type checking:
  `uv run ruff check . && uv run ruff format --check . && uv run mypy src/`
- Type hints on all public functions
- Docstrings on all public functions (Google style)

## Pull request process

1. Fork the repo and create a branch from `main`
2. Make your changes
3. Run `uv run ruff check . && uv run ruff format --check . && uv run mypy src/ && uv run pytest`
4. Open a PR with a clear description of what and why
5. Wait for review — we aim to respond within a few days

## Code of conduct

This project follows the Contributor Covenant v2.1.
Be kind, be respectful, be helpful.
