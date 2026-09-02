// Discord Ferry — Qwen settings builder, shared by installer and checker.
// install-local.mjs writes the result to .qwen/settings.json; check.mjs
// compares the file against a fresh build. One implementation, so the two
// sides cannot drift apart.

import { existsSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { qwenPostToolMatcher } from './hook-parity.mjs';

export function buildQwenSettings({
  projectRoot,
  home,
  templateDir,
  dataHome = process.env.XDG_DATA_HOME || join(home, '.local', 'share'),
}) {
  let content = readFileSync(join(templateDir, 'qwen-settings.json'), 'utf8');
  content = content.replaceAll('__PROJECT_ROOT__', projectRoot);
  content = content.replaceAll('__HOME__', home);
  content = content.replaceAll('__DATA_HOME__', dataHome);
  content = content.replaceAll('__QWEN_POST_TOOL_MATCHER__', qwenPostToolMatcher());
  const settings = JSON.parse(content);
  mergeQwenPromptHooks(settings, projectRoot);
  return settings;
}

// --- Prompt hook merge ----------------------------------------------------------
// Qwen supports prompt hooks natively. Their text is owned by plain-english
// (the docs and github judges) and by the local settings (verify reminder,
// compaction memory), so the builder copies them from .claude/settings.json
// and .claude/settings.local.json at build time instead of duplicating them
// in the template. Claude-only fields (model pin, continueOnBlock, if) are
// dropped: a Qwen prompt hook runs on the session model.

function collectClaudePromptHooks(projectRoot) {
  const hooks = [];
  for (const name of ['settings.json', 'settings.local.json']) {
    const settingsPath = join(projectRoot, '.claude', name);
    if (!existsSync(settingsPath)) continue;
    let raw;
    try {
      raw = JSON.parse(readFileSync(settingsPath, 'utf8'));
    } catch {
      continue;
    }
    for (const [event, groups] of Object.entries(raw.hooks ?? {})) {
      if (!Array.isArray(groups)) continue;
      for (const group of groups) {
        for (const hook of group.hooks ?? []) {
          if (hook.type !== 'prompt') continue;
          hooks.push({ event, matcher: group.matcher ?? null, hook });
        }
      }
    }
  }
  return hooks;
}

function qwenPromptHook(hook) {
  return { type: 'prompt', prompt: hook.prompt, timeout: hook.timeout ?? 30 };
}

// Maps a Claude prompt hook to the [event, matcher] of the .qwen/settings.json
// group it belongs to. Null when there is no Qwen home for it.
function qwenTargetFor(entry) {
  if (entry.event === 'PreToolUse') {
    if (entry.matcher?.includes('Bash')) return ['PreToolUse', 'run_shell_command'];
    if (entry.matcher?.includes('Write') || entry.matcher?.includes('Edit')) {
      return ['PreToolUse', 'write_file|edit'];
    }
    return null;
  }
  if (entry.event === 'PostToolUse') return ['PostToolUse', qwenPostToolMatcher()];
  if (entry.event === 'PreCompact') return ['PreCompact', null];
  return null;
}

function mergeQwenPromptHooks(settings, projectRoot) {
  let merged = 0;
  for (const entry of collectClaudePromptHooks(projectRoot)) {
    const target = qwenTargetFor(entry);
    if (!target) continue;
    const [event, matcher] = target;
    const groups = settings.hooks[event] ?? [];
    let group = groups.find(g => (g.matcher ?? null) === matcher);
    if (!group) {
      group = matcher ? { matcher, hooks: [] } : { hooks: [] };
      groups.push(group);
      settings.hooks[event] = groups;
    }
    group.hooks.push(qwenPromptHook(entry.hook));
    merged += 1;
  }
  return merged;
}
