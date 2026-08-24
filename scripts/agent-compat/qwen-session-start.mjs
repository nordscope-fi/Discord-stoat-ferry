#!/usr/bin/env node
// Discord Ferry — Qwen SessionStart context hook.
// Emits the session context (package version, recent git log, and the df-start
// nudge) as a hookSpecificOutput.additionalContext JSON block, the shape Qwen
// hooks parse. Claude Code accepts plain stdout for the same hooks; Qwen parses
// stdout as JSON, so this wrapper exists. The nudge itself stays in
// .claude/hooks/session-start-nudge.sh and keeps its own gating (it only prints
// on a fresh startup). See ADR-026.

import { execFileSync } from 'node:child_process';
import { existsSync, readFileSync } from 'node:fs';
import { join, resolve } from 'node:path';

const projectRoot = resolve(execFileSync('git', ['rev-parse', '--show-toplevel'], { encoding: 'utf8' }).trim());

let stdin = '';
try {
  stdin = readFileSync(0, 'utf8');
} catch {
  stdin = '{}';
}

const lines = [];

try {
  const version = readFileSync(join(projectRoot, 'src', 'discord_ferry', '__init__.py'), 'utf8');
  const match = version.match(/__version__\s*=\s*["']([^"']+)["']/);
  if (match) lines.push(`Discord Ferry v${match[1]}`);
} catch { /* version file not found */ }

try {
  const log = execFileSync('git', ['log', '--oneline', '-5'], {
    encoding: 'utf8', cwd: projectRoot, timeout: 5000,
  }).trim();
  if (log) lines.push(log);
} catch { /* git not available */ }

const nudgeScript = join(projectRoot, '.claude', 'hooks', 'session-start-nudge.sh');
if (existsSync(nudgeScript)) {
  try {
    const nudge = execFileSync('bash', [nudgeScript], {
      input: stdin, encoding: 'utf8', timeout: 5000,
    }).trim();
    if (nudge) lines.push(nudge);
  } catch { /* fail-open: no nudge is not an error */ }
}

if (lines.length === 0) process.exit(0);

process.stdout.write(JSON.stringify({
  hookSpecificOutput: {
    hookEventName: 'SessionStart',
    additionalContext: lines.join('\n'),
  },
}));
