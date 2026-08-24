#!/usr/bin/env node
// Discord Ferry — destructive git guard for hosts without a $TOOL_INPUT env var.
// Claude Code runs this check inline in .claude/settings.local.json using $TOOL_INPUT;
// Qwen passes the hook payload on stdin only, so this script reads the same envelope
// there. Blocks (exit 2, reason on stderr) when tool_input.command is a destructive
// git operation; fails open on anything unexpected. Detection is shared with the
// Codex and Vibe adapters (destructive-git.mjs). See ADR-026.
//
// Usage (registered as a PreToolUse command hook on run_shell_command):
//   echo '<hook json>' | node scripts/agent-compat/destructive-git-guard.mjs

import { readFileSync } from 'node:fs';
import { isDestructiveGitCommand } from './destructive-git.mjs';

let input;
try {
  input = JSON.parse(readFileSync(0, 'utf8'));
} catch {
  // Fail open: no parseable payload means nothing to judge.
  process.exit(0);
}

const cmd = input?.tool_input?.command ?? '';
if (isDestructiveGitCommand(cmd)) {
  process.stderr.write('Destructive git operation. Confirm with the user first.\n');
  process.exit(2);
}
process.exit(0);
