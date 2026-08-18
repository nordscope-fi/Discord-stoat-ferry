#!/usr/bin/env node
// Discord Ferry — Codex CLI hook adapter.
// Routes Codex lifecycle events to the shared guard scripts at ~/.claude/hooks/.
// Invoked by .codex/hooks.json for session-start, pre-tool, post-tool, and stop events.

import { execFileSync } from 'node:child_process';
import { existsSync, readFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { routesFor } from './hook-parity.mjs';

const mode = process.argv[2];
const projectRoot = resolve(execFileSync('git', ['rev-parse', '--show-toplevel'], { encoding: 'utf8' }).trim());
const userHooksDir = join(process.env.HOME, '.claude', 'hooks');

let input;
try {
  input = JSON.parse(readFileSync(0, 'utf8'));
} catch {
  input = {};
}

const contextLines = [];

// --- Hook execution -------------------------------------------------------------

function runHook(scriptPath, payload, collectOutput = false) {
  if (!existsSync(scriptPath)) return null;
  try {
    const result = execFileSync(scriptPath, [], {
      input: JSON.stringify(payload),
      encoding: 'utf8',
      timeout: 8000,
      env: { ...process.env, CLAUDE_PROJECT_DIR: projectRoot },
      stdio: ['pipe', 'pipe', 'pipe'],
    });
    if (collectOutput && result.trim()) {
      contextLines.push(result.trim());
    }
    return { ok: true, output: result.trim() };
  } catch (err) {
    if (err.status === 2) {
      const reason = (err.stderr || err.stdout || '').toString().trim() || 'blocked by guard';
      process.stdout.write(JSON.stringify({ decision: 'block', reason }));
      process.exit(0);
    }
    if (err.status !== 0) {
      process.stderr.write(`warning: ${scriptPath} exited ${err.status}\n`);
    }
    return { ok: false };
  }
}

function runUserHook(name, payload, collectOutput = false) {
  return runHook(join(userHooksDir, name), payload, collectOutput);
}

function runProjectHook(name, payload, collectOutput = false) {
  return runHook(join(projectRoot, '.claude', 'hooks', name), payload, collectOutput);
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

function runRoutes(event, toolName, payload) {
  const routes = routesFor(event, toolName);
  for (const route of routes) {
    runRoute(route, payload);
  }
}

// --- Destructive git check (inline, mirrors settings.local.json) ----------------

const DESTRUCTIVE_PATTERNS = [
  /\bgit\s+reset\s+--hard\b/,
  /\bgit\s+push\s+--force\b/,
  /\bgit\s+push\s+-f\b/,
  /\bgit\s+clean\s+-f/,
  /\bgit\s+branch\s+-D\b/,
  /\bgit\s+checkout\s+--\s+\.\s*$/,
  /\bgit\s+restore\s+\.\s*$/,
];

function checkDestructiveGit(payload) {
  const cmd = payload.tool_input?.command ?? payload.tool_input?.cmd ?? '';
  for (const pat of DESTRUCTIVE_PATTERNS) {
    if (pat.test(cmd)) {
      process.stdout.write(JSON.stringify({
        decision: 'block',
        reason: `Destructive git operation. Confirm with the user first.`,
      }));
      process.exit(0);
    }
  }
  return { ok: true };
}

// --- Edit path extraction -------------------------------------------------------

function editPaths(toolInput) {
  if (toolInput.file_path) return [toolInput.file_path];
  const cmd = toolInput.command ?? toolInput.patch ?? '';
  const paths = [];
  const re = /^\*{3}\s+(Add|Update|Delete)\s+File:\s+(.+)$/gm;
  let m;
  while ((m = re.exec(cmd)) !== null) {
    paths.push(m[2].trim());
  }
  return paths;
}

// --- Mode handlers --------------------------------------------------------------

function sessionStart() {
  try {
    const version = readFileSync(join(projectRoot, 'src', 'discord_ferry', '__init__.py'), 'utf8');
    const vMatch = version.match(/__version__\s*=\s*["']([^"']+)["']/);
    if (vMatch) contextLines.push(`Discord Ferry v${vMatch[1]}`);
  } catch { /* version file not found */ }

  try {
    const log = execFileSync('git', ['log', '--oneline', '-5'], {
      encoding: 'utf8', cwd: projectRoot, timeout: 5000,
    }).trim();
    if (log) contextLines.push(log);
  } catch { /* git not available */ }

  runProjectHook('session-start-nudge.sh', input, true);

  if (existsSync(join(userHooksDir, 'session-guard.sh'))) {
    runUserHook('session-guard.sh', input, true);
  }
  if (existsSync(join(userHooksDir, 'session-base.sh'))) {
    runUserHook('session-base.sh', input, true);
  }

  flushContext('SessionStart');
}

function preTool() {
  const tool = input.tool_name ?? '';
  const payload = { tool_name: tool, tool_input: input.tool_input ?? {} };

  if (tool === 'Bash' || tool === 'bash') {
    runRoutes('PreToolUse', 'Bash', payload);
    return;
  }

  if (tool === 'Read' || tool === 'read_file') {
    runRoutes('PreToolUse', 'Read', payload);
    return;
  }

  if (tool === 'apply_patch' || tool === 'Edit' || tool === 'Write' ||
      tool === 'edit' || tool === 'write_file') {
    const paths = editPaths(input.tool_input ?? {});
    for (const p of paths) {
      const editPayload = { ...payload, tool_input: { ...payload.tool_input, file_path: p } };
      runRoutes('PreToolUse', 'apply_patch', editPayload);
      runRoutes('PreToolUse', 'Edit', editPayload);
      runRoutes('PreToolUse', 'Write', editPayload);
    }
    return;
  }
}

function postTool() {
  const tool = input.tool_name ?? '';
  const payload = { tool_name: tool, tool_input: input.tool_input ?? {} };

  if (tool === 'apply_patch' || tool === 'Edit' || tool === 'Write' ||
      tool === 'edit' || tool === 'write_file') {
    const paths = editPaths(input.tool_input ?? {});
    for (const p of paths) {
      runRoute('qmd', { ...payload, tool_input: { ...payload.tool_input, file_path: p } });
    }
  }

  flushContext('PostToolUse');
}

function stopGuard() {
  const msg = input.last_assistant_message ?? '';
  const completionClaims = /\b(done|complete|fixed|shipped|resolved|finished|all\s+set)\b/i;
  const unfinishedLanguage = /\b(you can run|not yet tested|remaining|follow-up|future work|I'll leave|TODO|FIXME)\b/i;

  if (completionClaims.test(msg) && unfinishedLanguage.test(msg)) {
    process.stdout.write(JSON.stringify({
      decision: 'block',
      reason: 'Completion claimed but unfinished-work language detected. Finish the work, file it, or close the task.',
    }));
    process.exit(0);
  }
}

// --- Context flush --------------------------------------------------------------

function flushContext(eventName) {
  if (contextLines.length === 0) return;
  process.stdout.write(JSON.stringify({
    hookSpecificOutput: {
      hookEventName: eventName,
      additionalContext: contextLines.join('\n'),
    },
  }));
}

// --- Main -----------------------------------------------------------------------

switch (mode) {
  case 'session-start': sessionStart(); break;
  case 'pre-tool': preTool(); break;
  case 'post-tool': postTool(); break;
  case 'stop': stopGuard(); break;
  default:
    process.stderr.write(`codex-hook-adapter: unknown mode "${mode}"\n`);
    process.exit(1);
}
