"""Entry-point dispatch and console acquisition for the frozen binary.

PyInstaller's entry script is gui.py, whose main() never read sys.argv, so the
packaged Ferry.exe discarded every argument including --help (issue #123). It is
also built with console=False, which means that on Windows sys.stdout and
sys.stderr are None and no console is attached, so dispatching alone would still
produce no output. Both concerns live here to keep ctypes out of the UI module.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, NoReturn

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["acquire_console", "run_cli", "should_run_cli"]

_MP_FORK_FLAG = "--multiprocessing-fork"


def _stdout_is_usable() -> bool:
    """Whether sys.stdout can actually be written to.

    In a PyInstaller `console=False` build sys.stdout is None, unless the parent
    supplied a handle. That happens under redirection, pipes, and CI.
    """
    stream = sys.stdout
    return stream is not None and hasattr(stream, "write")


def _attach_parent_console() -> bool:
    """Attach to the launching shell's console and rebind the std streams.

    Windows only. Returns False when there is no parent console to attach to,
    such as a file dropped on the exe or a launch from Explorer.

    CONIN$ matters as much as CONOUT$: full delegation exposes `migrate` and
    `rollback`, and both prompt through click.confirm. Without stdin they would
    hang on a prompt the user cannot see, which is worse than the current
    behaviour of producing no output at all.
    """
    import ctypes

    attach_parent_process = -1
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]  # win32-only attribute
    if not kernel32.AttachConsole(attach_parent_process):
        return False

    try:
        sys.stdout = open("CONOUT$", "w", encoding="utf-8", buffering=1)  # noqa: SIM115
        sys.stderr = open("CONOUT$", "w", encoding="utf-8", buffering=1)  # noqa: SIM115
        sys.stdin = open("CONIN$", encoding="utf-8")  # noqa: SIM115
    except OSError:
        return False
    return True


def acquire_console() -> bool:
    """Ensure this process can print, returning False when it cannot.

    Three steps, in this order:

    1. sys.stdout already usable, so use it (redirection, pipes, CI)
    2. Windows: AttachConsole, then reopen CONOUT$ and CONIN$
    3. otherwise, no console is available

    Step 1 must precede step 2. Attaching to a parent console when the caller
    redirected our output would overwrite that redirection.
    """
    if _stdout_is_usable():
        return True
    if sys.platform != "win32":
        # A POSIX binary launched from a shell inherits working streams. If it
        # did not, there is nothing platform-specific left to try.
        return True
    return _attach_parent_console()


def should_run_cli(argv: Sequence[str]) -> bool:
    """Whether this process should hand off to the Click CLI.

    Four conditions, all required:

    1. Arguments were actually passed.
    2. The process is FROZEN. gui.main() is also the entry point for the
       `ferry-gui` and `ferry-desktop` console scripts, and for pytest, where
       sys.argv routinely carries unrelated arguments.
    3. The argv is not the pywebview spawn child's. freeze_support() exits
       first in production, so this is belt and braces.
    4. A console is available. See acquire_console(). Running a CLI whose output
       goes nowhere is the defect being fixed, not the fix.
    """
    if len(argv) <= 1:
        return False
    if not getattr(sys, "frozen", False):
        return False
    if _MP_FORK_FLAG in argv:
        return False
    return acquire_console()


def run_cli() -> NoReturn:
    """Hand off to the Click CLI and exit the process.

    Delegates to the bound `cli_main.main()`, not `cli_main()`: only
    `Command.main()` carries Click's NoReturn overload, `Command.__call__` is
    typed `-> Any`. The import is local so a non-frozen GUI launch never pulls
    in the CLI's dependency surface.
    """
    from discord_ferry.cli import main as cli_main

    cli_main.main()
