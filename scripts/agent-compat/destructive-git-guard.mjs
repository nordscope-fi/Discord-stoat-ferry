#!/usr/bin/env node
// Discord Ferry — destructive git guard for hosts without a $TOOL_INPUT env var.
// Claude Code runs this check inline in .claude/settings.local.json using $TOOL_INPUT;
// Qwen passes the hook payload on stdin only, so this script reads the same envelope
// there. Blocks (exit 2, reason on stderr) when tool_input.command is a destructive
// git operation; fails open on anything unexpected. Mirrors checkDestructiveGit in
// codex-hook-adapter.mjs. See ADR-026.
//
// Usage (registered as a PreToolUse command hook on run_shell_command):
//   echo '<hook json>' | node scripts/agent-compat/destructive-git-guard.mjs

import { readFileSync } from 'node:fs';

const DESTRUCTIVE_PATTERNS = [
  /\bgit\s+reset\s+--hard\b/,
  /\bgit\s+push\s+--force\b/,
  /\bgit\s+push\s+-f\b/,
  /\bgit\s+clean\s+-f/,
  /\bgit\s+branch\s+-D\b/,
  /\bgit\s+checkout\s+--\s+\./,
  /\bgit\s+restore\s+\./,
];

let input;
try {
  input = JSON.parse(readFileSync(0, 'utf8'));
} catch {
  // Fail open: no parseable payload means nothing to judge.
  process.exit(0);
}

const cmd = input?.tool_input?.command ?? '';
if (typeof cmd !== 'string' || cmd === '') process.exit(0);

for (const pattern of DESTRUCTIVE_PATTERNS) {
  if (pattern.test(cmd)) {
    process.stderr.write('Destructive git operation. Confirm with the user first.\n');
    process.exit(2);
  }
}
process.exit(0);
