"""Test-only NiceGUI entry point for the `user` fixture.

`nicegui_reset_globals()` clears `Client.page_routes`, so Ferry's `@ui.page`
decorators must run again *inside* the simulation. NiceGUI's mechanism for that
is to `runpy` a main file, which this is.

Pointing the marker at the real `src/discord_ferry/gui.py` was rejected: runpy
would also execute `main()` -> `_run_gui()` -> `finally: _teardown_native_window()`,
which fires in CI (`uv sync --extra native`) but not on a dev machine without
pywebview. A test harness whose behaviour depends on an optional extra is a
harness that lies.
"""

from __future__ import annotations

import importlib

from nicegui import ui

import discord_ferry.gui

# Re-import for its side effect: the @ui.page decorators re-register the routes
# that nicegui_reset_globals() just cleared.
importlib.reload(discord_ferry.gui)

# A no-op under simulation (nicegui/ui_run.py short-circuits on
# helpers.is_user_simulation()), so this never starts a real server -- but it
# still installs the storage secret, without which app.storage.user raises.
ui.run(storage_secret="simulated-secret")
