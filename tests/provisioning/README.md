# Discord Test Server Provisioning

Standalone CLI for provisioning a Discord test server matching the fixture
spec for issue #35's "captured-real fixtures" work. **Human-run only —
never in CI**, per issue #35 design.

## Prerequisites

1. **Discord bot** registered in the [Discord Developer Portal][1]:
   - Create a new application, then add a bot to it
   - Reset & copy the bot token (it appears once)
   - Store the token in shared 1Password vault under "Discord Ferry test bot"
   - Bot needs `MANAGE_CHANNELS`, `SEND_MESSAGES`, and `MANAGE_THREADS` scopes
   - Either invite the bot to an existing guild (use the OAuth URL generator)
     OR plan to use `--create-guild` (requires bot in <10 guilds total)

2. **Environment variable**:
   ```bash
   export DISCORD_TEST_BOT_TOKEN="MTM0NTY3..."
   ```

3. **Python deps** (already in the repo's `uv sync`):
   - aiohttp, Click, dataclasses, pytest, pytest-asyncio, aioresponses

[1]: https://discord.com/developers/applications

## Subcommands

### `provision` — apply manifest to a guild

```bash
# Use an existing guild:
uv run python -m tests.provisioning.provision_test_server provision --guild-id 123456789

# Bootstrap a new guild (unverified bot must be in <10 guilds):
uv run python -m tests.provisioning.provision_test_server provision --create-guild "Test Server"

# Plan only, no writes:
uv run python -m tests.provisioning.provision_test_server provision --guild-id 123456789 --dry-run
```

Idempotent — re-running on the same guild creates only missing entities.

### `teardown` — delete marker-carrying entities

```bash
uv run python -m tests.provisioning.provision_test_server teardown --guild-id 123456789
# Prompts for confirmation; use --yes to skip:
uv run python -m tests.provisioning.provision_test_server teardown --guild-id 123456789 --yes
```

Deletes ONLY channels carrying the `[ferry-fixture]` marker in their topic.
Manually-created channels in the test guild are untouched. Does NOT delete
the guild itself.

### `verify` — read-only state check

```bash
uv run python -m tests.provisioning.provision_test_server verify --guild-id 123456789
```

Exit codes (grep-style):
- `0` — manifest matches live state exactly
- `1` — drift detected (diff printed to stderr)
- `2` — couldn't determine (auth, network, malformed manifest)

## Marker conventions

To enable idempotent re-runs, every entity this script creates is tagged:

- **Channel topics**: prefixed with `[ferry-fixture]`
- **Message content**: suffixed with `[ferry:<manifest-id>]`

These markers are intentionally visible — they survive every renderer (Discord
client, DCE export, fixture-file JSON). **Do NOT hand-edit these markers** in
the Discord UI; doing so will cause `provision` re-runs to create duplicates
(and `verify` will catch this as `extra_marker_entity` drift).

## The "no CI" rule

The `provision_test_server.py` CLI (the `provision` / `teardown` / `verify`
subcommands documented above) must NEVER run in CI per issue #35's design.
Reason: the DCE capture step that follows provisioning is fundamentally
human-run (DCE requires interactive authentication). Automating provisioning
without automating capture would be misleading — the captured fixtures could
drift from what we expect to capture.

This rule applies ONLY to the CLI; the unit tests in `tests/provisioning/
test_*.py` (`test_applier.py`, `test_bot_api.py`, `test_cli.py`) are
hermetic — they mock the Discord REST API via `aioresponses` and run in CI
alongside the rest of the suite without contacting any real guild.

## Manifest

The committed `fixture-spec.json` IS the fixture specification. To change
what gets provisioned, edit the JSON; the loader enforces invariants
(exactly 3 inline + 2 non-inline embed fields, etc.) so malformed edits
fail fast.
