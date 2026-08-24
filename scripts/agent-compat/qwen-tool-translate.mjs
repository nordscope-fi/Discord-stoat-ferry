#!/usr/bin/env node
// Discord Ferry — Qwen tool-name translator for plain-english guards.
// Qwen hooks send runtime tool ids (write_file, edit, run_shell_command), and
// plain-english's claude-code profile matches on Claude's names (Write, Edit,
// Bash). Without translation the docs and github gates silently allow every
// Qwen write. Measured: a Qwen-shaped payload passed the shim untouched while
// the Claude-shaped twin produced a finding.
//
// This wrapper rewrites tool_name in the stdin envelope, then pipes it to the
// target guard and passes the guard's stdout, stderr and exit code through.
// The reply envelope (hookSpecificOutput JSON, exit 2) is already something
// Qwen parses. Fail-open: an unparseable payload goes through untouched.
//
// Usage (registered in .qwen/settings.json):
//   node scripts/agent-compat/qwen-tool-translate.mjs <guard-script> [args...]

import { spawnSync } from 'node:child_process';
import { readFileSync } from 'node:fs';

const QWEN_TO_CLAUDE = Object.freeze({
  run_shell_command: 'Bash',
  read_file: 'Read',
  write_file: 'Write',
  edit: 'Edit',
  grep_search: 'Grep',
  glob: 'Glob',
  web_fetch: 'WebFetch',
});

const [target, ...args] = process.argv.slice(2);
if (!target) {
  // Nothing to run means nothing to gate.
  process.exit(0);
}

let raw = '';
try {
  raw = readFileSync(0, 'utf8');
} catch {
  raw = '';
}

let input = raw;
try {
  const payload = JSON.parse(raw);
  const translated = payload && QWEN_TO_CLAUDE[payload.tool_name];
  if (translated) {
    payload.tool_name = translated;
    input = JSON.stringify(payload);
  }
} catch {
  // Not JSON: pass through untouched; the guard decides.
}

const run = spawnSync(target, args, {
  input,
  encoding: 'utf8',
  stdio: ['pipe', 'inherit', 'inherit'],
});
process.exit(run.status ?? 0);
