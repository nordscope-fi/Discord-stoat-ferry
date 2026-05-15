# Design: Fix xdg-open on Windows (issue #38)

**Date:** 2026-05-15 (revised 2026-05-16)
**Issue:** [#38](https://github.com/nordscope-fi/discord-stoat-ferry/issues/38)
**Ships as:** Single PR
**Status:** Spec — awaiting implementation

## Revisions from critique pass (2026-05-16)

The critique pass identified that this spec was over-scoped — the brainstorming-driven "audit everything" framing was empirically unfounded. Resolutions:

| Critique finding | Resolution in this revision |
|------------------|---------------------------|
| Audit found nothing actionable | **Re-scoped to "Fix xdg-open + commit-message audit summary" — codebase is already Windows-clean** |
| `os.startfile` test assertion fragile | Use `Path` directly; assert on `Path` not str |
| Coordination with #23 is a merge-conflict trap | Explicit protocol documented |
| Manual smoke test owner unspecified | Either named owner OR downgraded to "deferred verification" |
| Helper extraction was speculative | Inlined in `gui.py` (no other call sites) |
| Path-with-spaces verification missing | Invariant documented |

## Problem

`src/discord_ferry/gui.py:1020-1021` does:

```python
opener = "open" if sys.platform == "darwin" else "xdg-open"
subprocess.Popen([opener, str(report_path[0])])
```

On Windows, `sys.platform == "win32"` so `opener = "xdg-open"`, which doesn't exist. Reports can't be opened from the GUI on Windows. This raises and is caught by the surrounding `except Exception`, surfacing as a `ui.notify(...)` error — non-fatal but the feature is dead on Windows.

## Audit results (re-run during this revision)

The original brainstorming hypothesized "this bug is one instance of a pattern; audit `src/discord_ferry/` for similar Windows-incompatible code paths." Critique called this speculative; the audit greps were re-run during this revision and the codebase is essentially Windows-clean. Results below are evidence — not inferred.

| Pattern | Command | Hits | Disposition |
|---------|---------|------|-------------|
| `subprocess.Popen` / `subprocess.run` / `asyncio.subprocess` / `asyncio.create_subprocess_exec` | `grep -rn "subprocess\.\\|asyncio.create_subprocess" src/discord_ferry/ --include="*.py"` | 5 lines across 3 sites | `runner.py:123` (subprocess_exec) and `manager.py:107` (`detect_dotnet`) — **owned by #23**, no-flash via `creationflags=CREATE_NO_WINDOW`. `gui.py:1021` — **the bug**. No other sites. |
| `os.fork` / `os.execv` | `grep -rn "os\.fork\\|os\.execv" src/discord_ferry/ --include="*.py"` | 0 | Nothing to fix. |
| `signal.SIG*` | `grep -rn "signal\.SIG" src/discord_ferry/ --include="*.py"` | 0 | Nothing to fix. |
| `os.path.sep` / `os.sep` / hardcoded path separators | `grep -rn "os\.\\(path\\.\\)\\?sep" src/discord_ferry/ --include="*.py"` | 0 | Nothing to fix. (All path manipulation goes through `pathlib.Path`.) |
| `xdg-open` literal | `grep -rn "xdg-open" src/discord_ferry/ --include="*.py"` | 1 (the bug) | Fixed by this PR. |
| `chmod` / `0o7*` | `grep -rn "chmod\\\|0o7" src/discord_ferry/ --include="*.py"` | 1 (`manager.py:238`) | **Already gated** by `if platform.system() != "Windows":` (manager.py:237). No change needed. |
| `shell=True` | `grep -rn "shell=True" src/discord_ferry/ --include="*.py"` | 0 | Nothing to fix. |
| `read_text` / `write_text` without `encoding=` | `grep -rn "read_text\\\|write_text" src/discord_ferry/ --include="*.py"` | 14 hits across 8 files; **all 14 explicitly pass `encoding="utf-8"`** | Nothing to fix. |
| `open()` without `encoding=` | `grep -rn "open(" src/discord_ferry/ --include="*.py" \| grep -v "encoding=" \| grep -v "subprocess"` | 2 actionable hits: `uploader/autumn.py:82` (`open("rb")` — binary mode, no encoding needed) and `parser/dce_parser.py:116` (`open(json_path, "rb")` — binary mode) | Nothing to fix. |

**Net audit finding: zero new issues outside the known xdg-open bug.** The PR scope is one function rewrite + 2 tests + a commit-message summary documenting the audit results above.

## Architecture

### The fix (single function, inline in `gui.py`)

Replace the buggy 2-line snippet with a 3-branch platform check, **inlined** in `gui.py:_open_report`. No new module — audit confirmed zero other callers needing this helper.

```python
# src/discord_ferry/gui.py — replace lines 1017-1023:
def _open_report() -> None:
    if not report_path:
        return
    try:
        path = report_path[0]  # already a Path; do not coerce to str
        if sys.platform == "win32":
            os.startfile(path)  # Python 3.8+ accepts Path objects
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception as exc:
        ui.notify(f"Could not open report: {exc}", type="negative")
```

Notes on the implementation:
- `os.startfile` accepts `Path` objects since Python 3.8 (we're on 3.11+). No `str()` coercion needed.
- `subprocess.Popen` accepts `Path` objects in its argv list. No `str()` coercion needed.
- Imports already present in `gui.py`: `os`, `sys`, `subprocess`. No new imports.

### Path-with-spaces invariant

**All three branches handle paths containing spaces correctly** (e.g. `C:\Users\Pete Sterkenburg\report.html`) because none invoke a shell:

- `os.startfile(path)` — calls `ShellExecuteW` directly with the path as a parameter; no shell parsing.
- `subprocess.Popen(["open", path])` with a list argv (and no `shell=True`) — `argv[1]` is passed verbatim to `execve`; no shell parsing.
- `subprocess.Popen(["xdg-open", path])` — same.

**Do not "simplify" any branch into a string command** (e.g. `subprocess.Popen(f"open '{path}'")`, `subprocess.Popen(..., shell=True)`, or invoking `cmd /c start <path>` as a single string). String-form commands invoke shell parsing and re-introduce the spaces-in-path bug class. This invariant is documented here so future refactors don't regress.

## Components

| Component | Responsibility |
|-----------|----------------|
| `src/discord_ferry/gui.py` (FIXED) | `_open_report` rewritten with three-platform branch. No new helper module. |
| `tests/test_gui.py` (UPDATED) — or new `tests/test_open_report.py` if `_open_report` needs to be extracted to a module-level function for testability | Three tests: one per platform branch. |

## Data flow

```
User clicks "Open report" in GUI
    ↓
gui.py:_open_report() runs
    ↓
sys.platform branches to one of:
    - win32   → os.startfile(report_path)
    - darwin  → subprocess.Popen(["open", report_path])
    - other   → subprocess.Popen(["xdg-open", report_path])
    ↓
OS opens file in default handler
```

## Error handling

- All three branches are wrapped in `try/except Exception` (preserves current behavior).
- Failure surfaces as a `ui.notify(..., type="negative")` toast in the GUI.
- `os.startfile` may raise `FileNotFoundError` if the path doesn't exist or `OSError` for unsupported file types — caught by the same try/except.
- `subprocess.Popen` may raise `FileNotFoundError` if `open` / `xdg-open` not in PATH — caught.

## Testing

Three unit tests, one per platform branch. Tests must:
- Use `Path` objects (not strings) and assert on the `Path` argument that was passed.
- Use `monkeypatch.setattr(..., raising=False)` for `os.startfile` (the attribute doesn't exist on non-Windows hosts).

`_open_report` is currently a closure inside the GUI builder. To make it testable, the implementation should extract it to a module-level function `_open_path(path: Path) -> None` and have the closure delegate to it. Tests target `_open_path`.

```python
# tests/test_gui_open_path.py
from pathlib import Path
from unittest.mock import MagicMock
from discord_ferry.gui import _open_path

def test_open_path_uses_startfile_on_windows(monkeypatch):
    monkeypatch.setattr("sys.platform", "win32")
    mock_startfile = MagicMock()
    # raising=False: os.startfile doesn't exist on macOS/Linux test hosts
    monkeypatch.setattr("os.startfile", mock_startfile, raising=False)
    p = Path("C:/Users/Pete/report.html")
    _open_path(p)
    mock_startfile.assert_called_once_with(p)  # Path object, not str

def test_open_path_uses_open_on_macos(monkeypatch):
    monkeypatch.setattr("sys.platform", "darwin")
    mock_popen = MagicMock()
    monkeypatch.setattr("subprocess.Popen", mock_popen)
    p = Path("/Users/Pete/report.html")
    _open_path(p)
    mock_popen.assert_called_once_with(["open", p])  # Path, not str

def test_open_path_uses_xdg_open_on_linux(monkeypatch):
    monkeypatch.setattr("sys.platform", "linux")
    mock_popen = MagicMock()
    monkeypatch.setattr("subprocess.Popen", mock_popen)
    p = Path("/home/pete/report.html")
    _open_path(p)
    mock_popen.assert_called_once_with(["xdg-open", p])
```

Asserting on the `Path` object directly avoids Windows/POSIX string round-trip differences (the original spec mocked `sys.platform` but compared against a POSIX-style `"/test/path"` literal — that string doesn't equal `str(Path("/test/path"))` on a Windows test host). With `Path` equality, the test is platform-stable.

### Manual smoke test — deferred verification

No maintainer currently has a Windows machine for manual verification. **This PR ships with deferred Windows verification.** The unit tests prove the platform branch dispatches correctly; the actual `os.startfile` call cannot be unit-tested off-Windows.

Acceptance gate: rely on the first user-report on Windows after merge to confirm the fix works. If a Windows-machine maintainer becomes available before merge, run the manual smoke test:

1. Build Ferry GUI on Windows.
2. Run an export to completion.
3. Click "Open report" — confirm file opens in OS default handler.
4. Repeat with a path containing spaces (e.g. via `%USERPROFILE%\Documents\` for a user with a space in their name).

This is honest rather than aspirational: a manual smoke test with no named owner is not verification, just a wish.

## Phasing

Single PR. Smallest atom is "rewrite `_open_report` + three tests + commit-message audit summary."

## Coordination protocol with #23

#23 (DCE parser rewrite + cancel cleanup) modifies subprocess invocations at `runner.py:123-126` and `manager.py:107-118` to add `creationflags=subprocess.CREATE_NO_WINDOW` on Windows (avoids console-window flash).

**#38's PR must NOT touch `runner.py:123` or `manager.py:107`.** Audit confirms these are #23's territory and the `_open_report` fix in `gui.py` is fully orthogonal.

Sequencing rules:

| Order | Action |
|-------|--------|
| **#23 ships first** | #38 audit confirms only the xdg-open fix is needed; no subprocess scope changes. Ship as-is. |
| **#38 ships first** | Commit message and PR description explicitly state: "Subprocess audit deferred to #23 (which adds `CREATE_NO_WINDOW` to runner.py and manager.py). This PR touches only gui.py." When #23 lands, no merge conflict is expected because file regions don't overlap — but #38's commit message provides the audit-trail breadcrumb. |
| **Both ship same day** | Whoever lands second rebases; no logical conflict expected. |

## Decision Accountability gate

| Claim | Verdict | Evidence |
|-------|---------|----------|
| "Just fix xdg-open" (one-line fix would suffice) | **VERIFIED** (revised) | Audit re-run on 2026-05-16 found zero other Windows-incompatible patterns. Original brainstorming claim "likely more bugs of this pattern" was speculative and was falsified by the actual greps. |
| "stdlib `os.startfile` is right idiom on Windows" | VERIFIED | Avoids `webbrowser` dependency; matches double-click UX; accepts `Path` objects (Python ≥3.8). |
| "Audit needs to ship with fix" | DOWNGRADED | Original framing was wrong (audit found nothing). What ships with the fix is a **commit-message summary of the audit results above**, so future readers don't redo the work. |
| "CI Windows runner needed for verification" | DEFERRED | Substantial CI work; manual smoke test deferred to first user-report on Windows post-merge. See Deferrals. |
| "Manual smoke test acceptable for v1" | DOWNGRADED to "deferred verification" | No named Windows-machine owner; honesty preferred over aspiration. |

**Foundational?** No. Small Windows-specific bugfix. The audit was structurally interesting in principle but empty in practice.

## Risks

| Risk | Mitigation |
|------|------------|
| Fix ships untested on actual Windows; `os.startfile` behaves unexpectedly | Unit tests verify dispatch; first user-report serves as acceptance gate; failure surface is non-fatal `ui.notify` toast (current behavior). |
| `os.startfile` is Windows-only — `monkeypatch.setattr("os.startfile", ...)` fails on macOS/Linux test hosts | Use `raising=False` flag on monkeypatch (silently no-ops if attribute doesn't exist). |
| Future refactor "simplifies" the branch into a shell-string command, regressing spaces-in-path | Path-with-spaces invariant explicitly documented in the Architecture section above. |
| #23 lands first and silently changes the subprocess scope assumed by this audit | Coordination protocol documented above; audit results table includes commit-SHA reference (to be filled in at PR-write time) so #23's effect is verifiable. |

## Deferrals

| Item | Why not now | Tracking | Trigger |
|------|-------------|----------|---------|
| CI Windows-latest smoke test workflow | Substantial CI/cost addition; unclear ROI without a baseline of Windows users | (will file as follow-up if needed) | If Windows-specific regressions slip through after #38 merges |
| Cross-platform behavior testing via tox / nox matrix | Significant tooling change | — | If multi-version testing needs grow |
| Windows-specific GUI testing (PyAutoGUI / Playwright) | Out of scope | — | Never likely without significant Windows user base |
| Manual smoke test on actual Windows machine | No maintainer with a Windows machine | — | First user-report on Windows confirms or refutes the fix |

## Open questions

None blocking. The originally-listed questions ("extract to `_platform.py`?", "use `webbrowser` Linux fallback?") are resolved:
- **Extract to `_platform.py`?** → No. Audit confirms zero other call sites.
- **`webbrowser` fallback for headless Linux?** → No. Preserve current behavior; users without `xdg-open` can set `BROWSER` env var (Python's `webbrowser` module honors this) — but that's a separate issue if it ever surfaces.

## Cross-references

- **#23** (DCE parser rewrite) modifies subprocess invocations at `runner.py:123` and `manager.py:107` to add `creationflags=subprocess.CREATE_NO_WINDOW`. **Coordination protocol documented above.** This PR is **not** independent of #23 — the audit results table is only valid if `runner.py` and `manager.py` subprocess scope is unchanged from current `main`.
- Independent of #34 / #35 / #36 / #37 / #39.
