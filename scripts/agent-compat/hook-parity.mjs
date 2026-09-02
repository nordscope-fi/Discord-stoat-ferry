// Discord Ferry — Hook parity ledger.
// Maps every Claude Code hook registration to its Codex, Vibe and Qwen disposition.
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

// --- Qwen tool name translation -------------------------------------------------
// Qwen hooks match on runtime tool ids and speak the Claude hook envelope, so the
// guard scripts themselves run unchanged; only the matcher names differ.

const QWEN_TOOL_MAP = Object.freeze({
  Bash: 'run_shell_command',
  Read: 'read_file',
  Write: 'write_file',
  Edit: 'edit',
  WebFetch: 'web_fetch',
  apply_patch: null,
});

// --- Entry factories ------------------------------------------------------------

function projectCommand(
  id, event, matcher, commandId, disposition, codexTools, route, control,
  nativeHosts = [], hostOverrides = {},
) {
  return Object.freeze({
    id, source: 'project', event, matcher, handlerType: 'command', commandId,
    sha256: null, disposition, codexTools: Object.freeze(codexTools),
    codexMatcher: hostOverrides.codexMatcher ?? null,
    route, control: control ?? null, evidence: null, nativeHosts: Object.freeze(nativeHosts),
    hostOverrides: Object.freeze({ ...hostOverrides }),
  });
}

function userCommand(
  id, event, matcher, commandId, sha256Hash, disposition, codexTools, route, control,
  nativeHosts = [],
) {
  return Object.freeze({
    id, source: 'user', event, matcher, handlerType: 'command', commandId,
    sha256: sha256Hash, disposition, codexTools: Object.freeze(codexTools),
    route, control: control ?? null, evidence: null, nativeHosts: Object.freeze(nativeHosts),
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
  if (entry.hostOverrides?.vibeDisposition) {
    return {
      ...entry,
      vibeDisposition: entry.hostOverrides.vibeDisposition,
      vibeTools: Object.freeze(entry.hostOverrides.vibeTools ?? []),
    };
  }
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

// Qwen supports prompt hooks natively. Project prompt entries are compensated
// there: the installer merges them from .claude/settings.json at render time.
// User prompt entries stay unsupported: they live in ~/.claude/settings.json,
// which the Ferry installer does not write. Command entries with no Codex/Vibe
// equivalent (the plain-english chat gates) stay unsupported until plain-english
// ships a qwen agent profile.
function deriveQwenFields(entry) {
  if (entry.hostOverrides?.qwenDisposition) {
    return {
      ...entry,
      qwenDisposition: entry.hostOverrides.qwenDisposition,
      qwenTools: Object.freeze(entry.hostOverrides.qwenTools ?? []),
    };
  }
  if (entry.handlerType === 'prompt') {
    const qwenDisposition = entry.source === 'project' ? 'compensated' : 'unsupported';
    return { ...entry, qwenDisposition, qwenTools: Object.freeze([]) };
  }
  if (entry.disposition === 'unsupported') {
    return { ...entry, qwenDisposition: 'unsupported', qwenTools: Object.freeze([]) };
  }
  const qwenTools = entry.codexTools
    .map(t => (t in QWEN_TOOL_MAP ? QWEN_TOOL_MAP[t] : t))
    .filter(Boolean);
  const droppedByMap = entry.codexTools.length > 0 && qwenTools.length === 0;
  return {
    ...entry,
    qwenDisposition: droppedByMap ? 'unsupported' : entry.disposition,
    qwenTools: Object.freeze(qwenTools),
  };
}

// --- The ledger -----------------------------------------------------------------

const RAW_ENTRIES = [
  // Project hooks (from .claude/settings.json)
  projectCommand('project.plain-english-docs', 'PreToolUse', 'Write|Edit|MultiEdit',
    'plain-english-docs.sh', 'compensated', ['apply_patch', 'Edit', 'Write'], 'docs',
    'writing style guard for markdown edits', ['codex', 'vibe']),
  projectPrompt('project.plain-english-docs-prompt', 'PreToolUse', 'Write|Edit|MultiEdit',
    'LLM judge for writing style on markdown'),
  projectCommand('project.plain-english-github', 'PreToolUse', 'Bash',
    'plain-english-github.sh', 'compensated', ['Bash'], 'github-docs',
    'writing style guard for git/gh commands', ['codex', 'vibe']),
  projectPrompt('project.plain-english-github-prompt', 'PreToolUse', 'Bash',
    'LLM judge for writing style on git/gh'),
  projectCommand('project.plain-english-chat-stop', 'Stop', null,
    'plain-english-chat.sh', 'unsupported', [], null,
    'chat writing style; no Codex/Vibe equivalent'),
  projectCommand('project.plain-english-chat-subagent', 'SubagentStop', null,
    'plain-english-chat.sh', 'unsupported', [], null,
    'SubagentStop has no Codex/Vibe equivalent'),
  projectCommand('project.brainstorm-prompt', 'UserPromptSubmit', null,
    'brainstorm-evidence.mjs', 'ported', [], 'brainstorm-evidence',
    'activates or cancels the brainstorm evidence workflow', [],
    { vibeDisposition: 'unsupported', qwenDisposition: 'unsupported' }),
  projectCommand('project.brainstorm-before-tool', 'PreToolUse', '.*',
    'brainstorm-evidence.mjs', 'ported', [], 'brainstorm-evidence',
    'records eligible evidence before a tool runs', [],
    {
      codexMatcher: '.*',
      vibeDisposition: 'unsupported',
      qwenDisposition: 'unsupported',
    }),
  projectCommand('project.brainstorm-after-tool', 'PostToolUse', '.*',
    'brainstorm-evidence.mjs', 'ported', [], 'brainstorm-evidence',
    'completes eligible evidence after a tool succeeds', [],
    {
      codexMatcher: '.*',
      vibeDisposition: 'unsupported',
      qwenDisposition: 'unsupported',
    }),
  projectCommand('project.brainstorm-failed-tool', 'PostToolUseFailure', '.*',
    'brainstorm-evidence.mjs', 'compensated', [], 'brainstorm-evidence',
    'completes eligible challenge evidence after a tool fails', [],
    {
      vibeDisposition: 'unsupported',
      qwenDisposition: 'unsupported',
    }),
  projectCommand('project.brainstorm-stop', 'Stop', null,
    'brainstorm-evidence.mjs', 'ported', [], 'brainstorm-evidence',
    'blocks a recommendation until all evidence is complete', [],
    { vibeDisposition: 'unsupported', qwenDisposition: 'unsupported' }),

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
    'docs-plain-english-guard.sh', null, 'ported', ['apply_patch', 'Edit', 'Write'], 'docs', null,
    ['codex', 'vibe']),
  userPrompt('user.docs-prompt', 'PreToolUse', 'Write|Edit|MultiEdit',
    'LLM judge for docs plain english'),
  userCommand('user.github-command', 'PreToolUse', 'Bash',
    'github-plain-english-guard.sh', null, 'ported', ['Bash'], 'github-docs', null,
    ['codex', 'vibe']),
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

export const HOOK_PARITY = Object.freeze(RAW_ENTRIES.map(e => Object.freeze(deriveQwenFields(deriveVibeFields(e)))));

// --- Query helpers --------------------------------------------------------------

export function hostDisposition(entry, host) {
  if (host === 'vibe') return entry.vibeDisposition;
  if (host === 'qwen') return entry.qwenDisposition;
  return entry.disposition;
}

export function hostTools(entry, host) {
  if (host === 'vibe') return entry.vibeTools;
  if (host === 'qwen') return entry.qwenTools;
  return entry.codexTools;
}

export function entryMatchesTool(entry, toolName, host) {
  const matcher = host === 'codex' ? entry.codexMatcher ?? null : null;
  if (matcher !== null) {
    return typeof toolName === 'string' && new RegExp(`^(?:${matcher})$`, 'u').test(toolName);
  }
  const tools = hostTools(entry, host);
  return tools.length === 0
    || tools.includes(toolName)
    || entry.codexTools.includes(toolName);
}

export function routesFor(event, toolName, host, entries = HOOK_PARITY) {
  const routes = new Set();
  for (const e of entries) {
    if (e.event !== event) continue;
    const disposition = hostDisposition(e, host);
    if (disposition !== 'ported' && disposition !== 'compensated') continue;
    if (!e.route) continue;
    if (e.nativeHosts?.includes(host)) continue;
    if (!entryMatchesTool(e, toolName, host)) continue;
    routes.add(e.route);
  }
  return [...routes];
}

export function codexPostToolMatcher(entries = HOOK_PARITY) {
  const tools = new Set();
  const matchers = new Set();
  for (const e of entries) {
    if (e.event !== 'PostToolUse') continue;
    if (hostDisposition(e, 'codex') !== 'ported') continue;
    if (e.codexMatcher != null) matchers.add(e.codexMatcher);
    for (const t of e.codexTools) tools.add(t);
  }
  const alternatives = [
    ...[...tools].map(tool => tool.replace(/[.*+?^${}()|[\]\\]/gu, '\\$&')),
    ...matchers,
  ];
  return alternatives.length > 0 ? `^(${alternatives.join('|')})$` : '^$';
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

// Qwen matchers are Claude-style alternations of runtime tool ids, unanchored.
export function qwenPostToolMatcher(entries = HOOK_PARITY) {
  const tools = new Set();
  for (const e of entries) {
    if (e.event !== 'PostToolUse') continue;
    if (e.qwenDisposition !== 'ported') continue;
    for (const t of e.qwenTools) tools.add(t);
  }
  return tools.size > 0 ? [...tools].join('|') : '(?!)';
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
    if (!['ported', 'compensated', 'unsupported'].includes(e.qwenDisposition)) {
      issues.push(`${e.id}: invalid qwenDisposition "${e.qwenDisposition}"`);
    }
    if (e.nativeHosts && !e.nativeHosts.every(host => ['codex', 'vibe'].includes(host))) {
      issues.push(`${e.id}: invalid native host`);
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
