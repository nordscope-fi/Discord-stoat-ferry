---
globs:
  - "src/**/*.py"
  - "pyproject.toml"
---

# Context7 — Live Library Documentation

Before writing any API code that uses these libraries, fetch live documentation via Context7 MCP to verify current API signatures:

- **stoat-py** — `stoat` module: Client, HTTPClient, Permissions, MessageMasquerade, Reply, SendableEmbed, ChannelType
- **NiceGUI** — `nicegui` module: ui components, native mode, progress bars, file dialogs
- **Click** — CLI decorators, options, arguments, groups
- **Rich** — Progress bars, live display, console, tables

## Library ID Lookup Table

Use known IDs directly with `mcp__plugin_context7_context7__query-docs` to skip the resolve step:

| Library | Context7 ID | When to check |
|---|---|---|
| NiceGUI | `/websites/nicegui_io` | Before any UI component work |
| aiohttp | *resolve at use time* | Before HTTP client changes |
| Click | *resolve at use time* | Before CLI changes |
| Rich | *resolve at use time* | Before CLI output changes |
| pytest | *resolve at use time* | Before test infrastructure changes |
| stoat.py | *resolve at use time* | Before Stoat API wrapper changes |

For libraries marked "resolve at use time", use `mcp__plugin_context7_context7__resolve-library-id` first, then `mcp__plugin_context7_context7__query-docs`.

For libraries with a known ID, call `mcp__plugin_context7_context7__query-docs` directly with the ID.

## When

- Before implementing any new stoat.py API call (especially less common ones like `edit_category`, `create_emoji`, `pin_message`)
- Before implementing NiceGUI screens (verify component APIs match current version)
- When encountering unexpected behavior from a library call
- When the brief says "verify at implementation time" (see brief §15)
