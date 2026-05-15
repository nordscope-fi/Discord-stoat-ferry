# Design: Windows compatibility audit + xdg-open fix (issue #38)

**Date:** 2026-05-15
**Issue:** [#38](https://github.com/nordscope-fi/discord-stoat-ferry/issues/38)
**Ships as:** Single PR
**Status:** Spec — awaiting implementation (deferred to next session per user)

## Problem

`src/discord_ferry/gui.py:1021` runs `subprocess.Popen(["xdg-open", path])` on Windows where `xdg-open` doesn't exist. Reports can't be opened from the GUI on Windows.

Per brainstorming decision, scope expanded: this bug is one instance of "Ferry was developed primarily on macOS/Linux and has Windows-incompatible code paths that haven't been exercised." Audit `src/discord_ferry/` for similar issues; fix all in one pass.

## Architecture

Three-part change:

### 1. Audit (find all Windows-incompatible patterns)

Grep `src/discord_ferry/` for:

- `subprocess.Popen` / `subprocess.run` / `subprocess.call` / `asyncio.create_subprocess_*` — check each for:
  - `creationflags` on Windows (avoid console window flash)
  - Executable name (`xdg-open` vs `open` vs `os.startfile`)
  - `shell=True` (security + Windows behavior differ)
  - Hardcoded `/` paths in command arguments
- `os.path.sep` / `os.sep` / hardcoded `/` or `\\` in paths
- `os.fork` / `os.execv` / `signal.SIGUSR*` — POSIX-only APIs
- `open(file, mode="r")` without `encoding=` — Windows defaults to cp1252, causes UTF-8 misreads
- Hardcoded `~/.something` paths — Windows uses `%USERPROFILE%` (Path.home() abstracts this, but `os.path.expanduser` may behave differently)
- File permissions (`os.chmod`, `0o755`) — Windows ignores most of these
- Path separators in CLI flags (e.g., `--output ./out/` — Windows shell handles slash differently)

Findings documented inline as audit comments + commit message; no separate audit doc (smaller scope than #36).

### 2. Fix the xdg-open bug

```python
# src/discord_ferry/gui.py:1021 — replace:
import os, sys, subprocess

def _open_path(path: Path) -> None:
    """Open a file or directory in the OS default handler.

    Cross-platform: os.startfile on Windows, open on macOS, xdg-open on Linux.
    """
    if sys.platform == "win32":
        os.startfile(str(path))
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])
```

Extract to a helper so future opens use the same logic. Place in `src/discord_ferry/_platform.py` (NEW) or inline in gui.py — pick during writing-plans based on whether other modules need it.

### 3. Fix any other audit findings

Each finding fixed inline:
- Subprocess flash → add `creationflags=subprocess.CREATE_NO_WINDOW` on Windows.
- `open(...)` without encoding → add `encoding="utf-8"`.
- Hardcoded path separators → use `pathlib.PurePath` or `os.path.join`.
- POSIX-only APIs → platform-conditional with documented fallback.

## Components

| Component | Responsibility |
|-----------|----------------|
| `src/discord_ferry/_platform.py` (NEW, optional) | `_open_path(path)` helper if reused; otherwise inline in gui.py |
| `src/discord_ferry/gui.py` (FIXED) | xdg-open replaced with platform-aware open |
| Other src/ files (FIXED per audit) | Each Windows-incompatible pattern resolved |
| `tests/test_gui.py` or `tests/test_platform.py` (UPDATED/NEW) | Tests patching `sys.platform` for each platform branch |

## Data flow

User clicks "Open report" in GUI on Windows
    ↓
gui.py calls `_open_path(report_file)`
    ↓
sys.platform is "win32" → `os.startfile(str(report_file))`
    ↓
Windows opens file in default handler

## Error handling

- `os.startfile` may raise `FileNotFoundError` if path doesn't exist — preserve current behavior (try/except higher up if any; file an issue if observed).
- `subprocess.Popen` for non-Windows may fail if `open` / `xdg-open` not in PATH — preserve current behavior.

## Testing

Unit tests in `tests/test_gui.py` (or new `tests/test_platform.py`):

```python
def test_open_path_uses_startfile_on_windows(monkeypatch):
    monkeypatch.setattr("sys.platform", "win32")
    mock_startfile = MagicMock()
    monkeypatch.setattr("os.startfile", mock_startfile, raising=False)  # may not exist on non-Windows
    _open_path(Path("/test/path"))
    mock_startfile.assert_called_once_with("/test/path")

def test_open_path_uses_open_on_macos(monkeypatch):
    monkeypatch.setattr("sys.platform", "darwin")
    mock_popen = MagicMock()
    monkeypatch.setattr("subprocess.Popen", mock_popen)
    _open_path(Path("/test/path"))
    mock_popen.assert_called_once_with(["open", "/test/path"])

def test_open_path_uses_xdg_open_on_linux(monkeypatch):
    monkeypatch.setattr("sys.platform", "linux")
    mock_popen = MagicMock()
    monkeypatch.setattr("subprocess.Popen", mock_popen)
    _open_path(Path("/test/path"))
    mock_popen.assert_called_once_with(["xdg-open", "/test/path"])
```

For each other audit finding fixed: a similar platform-conditional test where applicable.

Manual smoke test (Windows):
- Build Ferry GUI on Windows.
- Run an export to completion.
- Click "Open report" — confirm file opens in OS default handler.

## Phasing

Single PR. Smallest atom is "audit + all fixes." Splitting into "fix xdg-open" + "audit" + "fix audit findings" creates ceremony for marginal benefit.

**Sequencing:** Independent of #23/#34/#35/#36/#37/#39. Can ship anytime.

## Decision Accountability gate

| Claim | Verdict | Evidence |
|-------|---------|----------|
| "Just fix xdg-open" (one-line fix) | FALSIFIED via brainstorming | Same anti-pattern as #36; one known instance + likely more |
| "stdlib `os.startfile` is right idiom" | VERIFIED | Avoids `webbrowser` dependency; matches double-click UX |
| "Audit needs to ship with fix" | VERIFIED | Otherwise the audit findings rot or get re-discovered |
| "CI Windows runner needed for verification" | DEFERRED | Substantial CI work; manual smoke test acceptable for v1; revisit later (see Deferrals) |

**Foundational?** No (small Windows-specific bugfix), but the audit is structurally important.

## Risks

| Risk | Mitigation |
|------|------------|
| Audit finds many Windows-breaking issues; PR balloons | Triage during audit: "must fix" vs "deferred to separate issue" |
| `os.startfile` is Windows-only — `monkeypatch.setattr("os.startfile", ...)` fails on macOS/Linux | Use `raising=False` flag on monkeypatch (does silently no-op if attribute doesn't exist) |
| Audit misses subtle issues that only manifest on Windows | Manual smoke test catches some; CI Windows runner deferred to separate issue |
| Subprocess flag changes cause unexpected behavior on macOS/Linux | All changes platform-conditional; non-Windows code paths unchanged |

## Deferrals

| Item | Why not now | Tracking | Trigger |
|------|-------------|----------|---------|
| CI Windows-latest smoke test workflow | Substantial CI/cost addition; unclear ROI without baseline | (will file as follow-up) | If Windows-specific regressions slip through |
| Cross-platform behavior testing via tox / nox matrix | Significant tooling change | — | If multi-version testing needs grow |
| Windows-specific GUI testing (PyAutoGUI / Playwright) | Out of scope | — | Never likely without significant Windows user base |

## Open questions (for implementation, not blocking spec)

- Whether to extract `_open_path` to a new module (`_platform.py`) or keep inline. Decide based on whether audit finds other modules that need it.
- Whether to use `webbrowser.open(file://...)` as a Linux fallback when `xdg-open` is missing (for headless Linux). Lean toward leaving current behavior; users without xdg-open should set `BROWSER` env var.

## Cross-references

- Issue #23 already adds `CREATE_NO_WINDOW` to two specific subprocess calls (runner.py and manager.py:detect_dotnet). #38's audit must NOT duplicate those changes. Coordinate sequencing: if #23 ships first, #38's audit confirms the runner/manager changes are sufficient and looks for OTHER subprocess gaps.
- Independent of #34/#35/#36/#37/#39.
