#!/usr/bin/env node
// Discord Ferry — Vibe CLI hook adapter.
// Routes Vibe lifecycle events to the shared guard scripts at ~/.claude/hooks/.
// Vibe blocks via stdout JSON {"decision":"deny","reason":"..."} + exit 0 (not exit 2).
// Invoked by .vibe/hooks.toml for pre-tool, post-tool, and post-agent events.

import { execFileSync } from 'node:child_process';
import { existsSync, readFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { routesFor } from './hook-parity.mjs';
import { isDestructiveGitCommand } from './destructive-git.mjs';

const mode = process.argv[2];
const projectRoot = resolve(execFileSync('git', ['rev-parse', '--show-toplevel'], { encoding: 'utf8' }).trim());
const userHooksDir = join(process.env.HOME, '.claude', 'hooks');

let input;
try {
  input = JSON.parse(readFileSync(0, 'utf8'));
} catch {
  input = {};
}

// --- Vibe tool name translation -------------------------------------------------

const VIBE_TO_CLAUDE = {
  bash: 'Bash',
  read_file: 'Read',
  write_file: 'Write',
  edit: 'Edit',
  web_fetch: 'WebFetch',
};

function translateTool(vibeName) {
  return VIBE_TO_CLAUDE[vibeName] ?? vibeName;
}

// --- Block protocol (Vibe uses deny + exit 0, not exit 2) -----------------------

function block(reason) {
  process.stdout.write(JSON.stringify({ decision: 'deny', reason }));
  process.exit(0);
}

// --- Hook execution -------------------------------------------------------------

function runHook(scriptPath, payload) {
  if (!existsSync(scriptPath)) return null;
  try {
    execFileSync(scriptPath, [], {
      input: JSON.stringify(payload),
      encoding: 'utf8',
      timeout: 8000,
      env: { ...process.env, CLAUDE_PROJECT_DIR: projectRoot },
      stdio: ['pipe', 'pipe', 'pipe'],
    });
    return { ok: true };
  } catch (err) {
    if (err.status === 2) {
      const reason = (err.stderr || err.stdout || '').toString().trim() || 'blocked by guard';
      block(reason);
    }
    if (err.status !== 0) {
      process.stderr.write(`warning: ${scriptPath} exited ${err.status}\n`);
    }
    return { ok: false };
  }
}

function runUserHook(name, payload) {
  return runHook(join(userHooksDir, name), payload);
}

function runProjectHook(name, payload) {
  return runHook(join(projectRoot, '.claude', 'hooks', name), payload);
}

// --- Route dispatch -------------------------------------------------------------

function runRoute(route, payload) {
  switch (route) {
    case 'credential':
      return runUserHook('credential-guard.sh', payload);
    case 'write':
      return runUserHook('write-guard.sh', payload);
    case 'branch':
      return runUserHook('branch-guard.sh', payload);
    case 'docs':
      runUserHook('docs-plain-english-guard.sh', payload);
      return runProjectHook('plain-english-docs.sh', payload);
    case 'github-docs':
      runUserHook('github-plain-english-guard.sh', payload);
      return runProjectHook('plain-english-github.sh', payload);
    case 'read':
      return runUserHook('read-guard.sh', payload);
    case 'qmd':
      return runUserHook('qmd-live-update.sh', payload);
    case 'destructive-git':
      return checkDestructiveGit(payload);
    default:
      return null;
  }
}

// --- Destructive git check (shared detection with the Qwen and Codex guards) ---

function checkDestructiveGit(payload) {
  const cmd = payload.tool_input?.command ?? payload.tool_input?.cmd ?? '';
  if (isDestructiveGitCommand(cmd)) {
    block('Destructive git operation. Confirm with the user first.');
  }
  return { ok: true };
}

// --- Edit path extraction -------------------------------------------------------

function editPaths(toolInput) {
  if (toolInput.file_path) return [toolInput.file_path];
  return [];
}

// --- Mode handlers --------------------------------------------------------------

function preTool() {
  const vibeTool = input.tool_name ?? '';
  const claudeTool = translateTool(vibeTool);
  const payload = { tool_name: claudeTool, tool_input: input.tool_input ?? {} };

  if (claudeTool === 'Bash') {
    const routes = routesFor('PreToolUse', 'Bash', 'vibe');
    for (const route of routes) runRoute(route, payload);
    return;
  }

  if (claudeTool === 'Read') {
    const routes = routesFor('PreToolUse', 'Read', 'vibe');
    for (const route of routes) runRoute(route, payload);
    return;
  }

  if (claudeTool === 'Write' || claudeTool === 'Edit') {
    const paths = editPaths(input.tool_input ?? {});
    for (const p of paths) {
      const editPayload = { ...payload, tool_input: { ...payload.tool_input, file_path: p } };
      const routes = [
        ...routesFor('PreToolUse', 'Write', 'vibe'),
        ...routesFor('PreToolUse', 'Edit', 'vibe'),
      ];
      const seen = new Set();
      for (const route of routes) {
        if (seen.has(route)) continue;
        seen.add(route);
        runRoute(route, editPayload);
      }
    }
    return;
  }
}

function postTool() {
  const vibeTool = input.tool_name ?? '';
  const claudeTool = translateTool(vibeTool);

  if (claudeTool === 'Write' || claudeTool === 'Edit') {
    const paths = editPaths(input.tool_input ?? {});
    for (const p of paths) {
      runRoute('qmd', {
        tool_name: claudeTool,
        tool_input: { ...(input.tool_input ?? {}), file_path: p },
      });
    }
  }
}

function postAgent() {
  const msg = input.last_assistant_message ?? '';
  const completionClaims = /\b(done|complete|fixed|shipped|resolved|finished|all\s+set)\b/i;
  const unfinishedLanguage = /\b(you can run|not yet tested|remaining|follow-up|future work|I'll leave|TODO|FIXME)\b/i;

  if (completionClaims.test(msg) && unfinishedLanguage.test(msg)) {
    block('Completion claimed but unfinished-work language detected. Finish the work, file it, or close the task.');
  }
}

// --- Main -----------------------------------------------------------------------

switch (mode) {
  case 'pre-tool': preTool(); break;
  case 'post-tool': postTool(); break;
  case 'post-agent': postAgent(); break;
  default:
    process.stderr.write(`vibe-hook-adapter: unknown mode "${mode}"\n`);
    process.exit(1);
}
