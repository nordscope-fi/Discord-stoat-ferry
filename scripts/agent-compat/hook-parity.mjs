// Discord Ferry — Hook parity ledger.
// Maps every Claude Code hook registration to its Codex and Vibe disposition.
// The adapter reads routesFor() at runtime; the verifier reads auditHookParity() at check time.

import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';

// --- Vibe tool name translation -------------------------------------------------

const VIBE_TOOL_MAP = Object.freeze({
  Bash: 'bash',
  Read: 'read_file',
  Write: 'write_file',
  Edit: 'edit',
  WebFetch: 'web_fetch',
  apply_patch: null,
});

// --- Entry factories ------------------------------------------------------------

function projectCommand(id, event, matcher, commandId, disposition, codexTools, route, control) {
  return Object.freeze({
    id, source: 'project', event, matcher, handlerType: 'command', commandId,
    sha256: null, disposition, codexTools: Object.freeze(codexTools),
    route, control: control ?? null, evidence: null,
  });
}

function userCommand(id, event, matcher, commandId, sha256Hash, disposition, codexTools, route, control) {
  return Object.freeze({
    id, source: 'user', event, matcher, handlerType: 'command', commandId,
    sha256: sha256Hash, disposition, codexTools: Object.freeze(codexTools),
    route, control: control ?? null, evidence: null,
  });
}

function projectPrompt(id, event, matcher, control) {
  return Object.freeze({
    id, source: 'project', event, matcher, handlerType: 'prompt', commandId: null,
    sha256: null, disposition: 'unsupported', codexTools: Object.freeze([]),
    route: null, control: control ?? null, evidence: 'prompt-type hooks require Claude model invocation',
  });
}

function userPrompt(id, event, matcher, control) {
  return Object.freeze({
    id, source: 'user', event, matcher, handlerType: 'prompt', commandId: null,
    sha256: null, disposition: 'unsupported', codexTools: Object.freeze([]),
    route: null, control: control ?? null, evidence: 'prompt-type hooks require Claude model invocation',
  });
}

function deriveVibeFields(entry) {
  if (entry.disposition === 'unsupported') {
    return { ...entry, vibeDisposition: 'unsupported', vibeTools: Object.freeze([]) };
  }
  const vibeTools = entry.codexTools
    .map(t => VIBE_TOOL_MAP[t] ?? t)
    .filter(Boolean);
  return {
    ...entry,
    vibeDisposition: vibeTools.length > 0 ? entry.disposition : 'unsupported',
    vibeTools: Object.freeze(vibeTools),
  };
}

// --- The ledger -----------------------------------------------------------------

const RAW_ENTRIES = [
  // Project hooks (from .claude/settings.json)
  projectCommand('project.plain-english-docs', 'PreToolUse', 'Write|Edit|MultiEdit',
    'plain-english-docs.sh', 'compensated', ['apply_patch', 'Edit', 'Write'], 'docs',
    'writing style guard for markdown edits'),
  projectPrompt('project.plain-english-docs-prompt', 'PreToolUse', 'Write|Edit|MultiEdit',
    'LLM judge for writing style on markdown'),
  projectCommand('project.plain-english-github', 'PreToolUse', 'Bash',
    'plain-english-github.sh', 'compensated', ['Bash'], 'github-docs',
    'writing style guard for git/gh commands'),
  projectPrompt('project.plain-english-github-prompt', 'PreToolUse', 'Bash',
    'LLM judge for writing style on git/gh'),
  projectCommand('project.plain-english-issue', 'PreToolUse',
    'mcp__linear__save_issue|mcp__linear__save_comment',
    'plain-english-issue.sh', 'unsupported', [], null,
    'Linear issue guard; Linear MCP not available on Codex/Vibe'),
  projectPrompt('project.plain-english-issue-prompt', 'PreToolUse',
    'mcp__linear__save_issue|mcp__linear__save_comment',
    'LLM judge for Linear issues'),
  projectCommand('project.plain-english-chat-stop', 'Stop', null,
    'plain-english-chat.sh', 'unsupported', [], null,
    'chat writing style; no Codex/Vibe equivalent'),
  projectCommand('project.plain-english-chat-subagent', 'SubagentStop', null,
    'plain-english-chat.sh', 'unsupported', [], null,
    'SubagentStop has no Codex/Vibe equivalent'),

  // Local hooks (from .claude/settings.local.json)
  projectCommand('local.destructive-git', 'PreToolUse', 'Bash',
    'inline-destructive-git', 'ported', ['Bash'], 'destructive-git',
    'blocks reset --hard, push --force, clean -fd, branch -D, checkout -- ., restore .'),
  projectPrompt('local.post-tool-verify', 'PostToolUse', 'Write|Edit',
    'verify suite reminder after edits'),
  projectPrompt('local.pre-compact', 'PreCompact', null,
    'memory persistence before compaction'),
  projectCommand('local.session-version', 'SessionStart', null,
    'inline-version-echo', 'ported', [], null,
    'echo package version + git log on session start'),
  projectCommand('local.session-nudge', 'SessionStart', null,
    'session-start-nudge.sh', 'ported', [], null,
    'session start context injection'),

  // User hooks (from ~/.claude/hooks/ — machine-global, shared across projects)
  userCommand('user.credential-bash', 'PreToolUse', 'Bash',
    'credential-guard.sh', null, 'ported', ['Bash'], 'credential', null),
  userCommand('user.write', 'PreToolUse', 'Write|Edit|MultiEdit',
    'write-guard.sh', null, 'ported', ['apply_patch', 'Edit', 'Write'], 'write', null),
  userCommand('user.branch', 'PreToolUse', 'Bash',
    'branch-guard.sh', null, 'ported', ['Bash'], 'branch', null),
  userCommand('user.docs-command', 'PreToolUse', 'Write|Edit|MultiEdit',
    'docs-plain-english-guard.sh', null, 'ported', ['apply_patch', 'Edit', 'Write'], 'docs', null),
  userPrompt('user.docs-prompt', 'PreToolUse', 'Write|Edit|MultiEdit',
    'LLM judge for docs plain english'),
  userCommand('user.github-command', 'PreToolUse', 'Bash',
    'github-plain-english-guard.sh', null, 'ported', ['Bash'], 'github-docs', null),
  userPrompt('user.github-prompt', 'PreToolUse', 'Bash',
    'LLM judge for github plain english'),
  userCommand('user.unfinished', 'Stop', null,
    'unfinished-guard.mjs', null, 'compensated', [], null,
    'adapter stop guard carries inline equivalent'),
  userCommand('user.qmd', 'PostToolUse', 'Write|Edit|MultiEdit',
    'qmd-live-update.sh', null, 'ported', ['apply_patch', 'Edit', 'Write'], 'qmd', null),
  userCommand('user.read', 'PreToolUse', 'Read',
    'read-guard.sh', null, 'ported', ['Read'], 'read', null),
];

export const HOOK_PARITY = Object.freeze(RAW_ENTRIES.map(e => Object.freeze(deriveVibeFields(e))));

// --- Query helpers --------------------------------------------------------------

export function routesFor(event, toolName, entries = HOOK_PARITY) {
  const routes = new Set();
  for (const e of entries) {
    if (e.event !== event) continue;
    if (e.disposition !== 'ported' && e.disposition !== 'compensated') continue;
    if (!e.route) continue;
    if (e.codexTools.length > 0 && !e.codexTools.includes(toolName)) continue;
    routes.add(e.route);
  }
  return [...routes];
}

export function codexPostToolMatcher(entries = HOOK_PARITY) {
  const tools = new Set();
  for (const e of entries) {
    if (e.event !== 'PostToolUse') continue;
    if (e.disposition !== 'ported') continue;
    for (const t of e.codexTools) tools.add(t);
  }
  return tools.size > 0 ? `^(${[...tools].join('|')})$` : '^$';
}

export function vibePostToolMatcher(entries = HOOK_PARITY) {
  const tools = new Set();
  for (const e of entries) {
    if (e.event !== 'PostToolUse') continue;
    if (e.vibeDisposition !== 'ported') continue;
    for (const t of e.vibeTools) tools.add(t);
  }
  return tools.size > 0 ? `re:^(${[...tools].join('|')})$` : 're:^$';
}

// --- Validation -----------------------------------------------------------------

export function sha256Sum(value) {
  return `sha256:${createHash('sha256').update(value).digest('hex')}`;
}

export function stableCommandId(command) {
  const scriptMatch = command.match(/[\w.-]+\.(sh|mjs)(?:\s|"|$)/);
  if (scriptMatch) return scriptMatch[0].trim().replace(/"/g, '');
  if (command.includes('TOOL_INPUT') && command.includes('case')) return 'inline-destructive-git';
  if (/^echo\b/.test(command) && command.includes('version')) return 'inline-version-echo';
  return command.split(/\s+/)[0];
}

export function registrationFingerprint({ source, event, matcher, handlerType, commandId }) {
  return `${source}:${event}:${matcher ?? '*'}:${handlerType}:${commandId ?? '-'}`;
}

export function extractHookRegistrations(source, settings) {
  const registrations = [];
  if (!settings) return registrations;
  for (const [event, groups] of Object.entries(settings)) {
    if (!Array.isArray(groups)) continue;
    for (const group of groups) {
      const matcher = group.matcher ?? null;
      const hooks = group.hooks ?? [];
      for (const hook of hooks) {
        const handlerType = hook.type ?? 'command';
        const commandId = handlerType === 'command' ? stableCommandId(hook.command ?? '') : null;
        registrations.push({ source, event, matcher, handlerType, commandId });
      }
    }
  }
  return registrations;
}

export function validateHookParity(entries = HOOK_PARITY) {
  const issues = [];
  const ids = new Set();
  for (const e of entries) {
    if (ids.has(e.id)) issues.push(`duplicate id: ${e.id}`);
    ids.add(e.id);
    if (!['ported', 'compensated', 'unsupported'].includes(e.disposition)) {
      issues.push(`${e.id}: invalid disposition "${e.disposition}"`);
    }
    if (e.disposition === 'ported' && !e.route && e.codexTools.length > 0) {
      issues.push(`${e.id}: ported with codexTools but no route`);
    }
  }
  return issues;
}

export function auditHookParity({ projectSettings, userSettings, userSettingsError, userScripts, strict, entries } = {}) {
  const failures = [];
  const warnings = [];
  const ent = entries ?? HOOK_PARITY;

  const structuralIssues = validateHookParity(ent);
  if (structuralIssues.length > 0) {
    failures.push(`structural: ${structuralIssues.join('; ')}`);
  }

  if (projectSettings) {
    const projectRegs = extractHookRegistrations('project', projectSettings);
    for (const reg of projectRegs) {
      const fp = registrationFingerprint(reg);
      const match = ent.find(e => registrationFingerprint(e) === fp);
      if (!match) {
        warnings.push(`project hook not in ledger: ${fp}`);
      }
    }
  }

  if (userSettings) {
    const userRegs = extractHookRegistrations('user', userSettings);
    for (const reg of userRegs) {
      const fp = registrationFingerprint(reg);
      const match = ent.find(e => registrationFingerprint(e) === fp);
      if (!match && strict) {
        warnings.push(`user hook not in ledger: ${fp}`);
      }
    }
  } else if (userSettingsError) {
    warnings.push(`could not read user settings: ${userSettingsError}`);
  }

  if (userScripts && strict) {
    for (const e of ent) {
      if (e.source !== 'user' || !e.sha256) continue;
      const actual = userScripts[e.commandId];
      if (!actual) {
        warnings.push(`${e.id}: user script "${e.commandId}" not found`);
      } else if (actual !== e.sha256) {
        warnings.push(`${e.id}: user script hash changed (expected ${e.sha256}, got ${actual})`);
      }
    }
  }

  return { failures, warnings };
}
