#!/usr/bin/env node
// Discord Ferry — Cross-platform agent config installer.
// Renders templates from config/agent-compat/ into .codex/, .vibe/ and .qwen/,
// creates .agents/skills/ symlinks via the skill topology bridger.
// Run: ./scripts/agent-install.sh (or: node scripts/agent-compat/install-local.mjs)

import { execFileSync, spawnSync } from 'node:child_process';
import { existsSync, mkdirSync, readdirSync, readFileSync, statSync, unlinkSync, writeFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { codexPostToolMatcher, qwenPostToolMatcher, vibePostToolMatcher } from './hook-parity.mjs';
import { buildSkillPlan, applySkillPlan } from './skill-topology.mjs';

const projectRoot = resolve(execFileSync('git', ['rev-parse', '--show-toplevel'], { encoding: 'utf8' }).trim());
const templateDir = join(projectRoot, 'config', 'agent-compat');
const home = process.env.HOME;

// --- Helpers --------------------------------------------------------------------

function render(templateName, extraReplacements = {}) {
  let content = readFileSync(join(templateDir, templateName), 'utf8');
  content = content.replaceAll('__PROJECT_ROOT__', projectRoot);
  content = content.replaceAll('__HOME__', home);
  for (const [key, value] of Object.entries(extraReplacements)) {
    content = content.replaceAll(key, value);
  }
  return content;
}

function reconcileDirectory(dirPath, expectedNames) {
  if (!existsSync(dirPath)) return;
  const actual = readdirSync(dirPath);
  for (const name of actual) {
    if (!expectedNames.includes(name)) {
      const fullPath = join(dirPath, name);
      // Directories belong to the agent or its tools (.qwen/worktrees,
      // .vibe/hooks); the generator owns files only.
      if (statSync(fullPath).isDirectory()) continue;
      unlinkSync(fullPath);
      console.log(`  removed unexpected: ${name}`);
    }
  }
}

function plainEnglishAvailable() {
  const result = spawnSync('plain-english', ['--version'], { encoding: 'utf8', stdio: 'pipe' });
  return result.status === 0;
}

function runPlainEnglishInit(agent) {
  try {
    execFileSync('plain-english', ['init', `--agent`, agent, '--root', projectRoot], {
      encoding: 'utf8',
      stdio: 'pipe',
    });
    console.log(`  plain-english init --agent ${agent}: merged lint hooks`);
  } catch (err) {
    console.error(`  plain-english init --agent ${agent} failed: ${err.message}`);
    console.error('  Run `npm install -g plain-english` and re-run ./scripts/agent-install.sh');
  }
}

function stripIssueChannel() {
  const codexPath = join(projectRoot, '.codex', 'hooks.json');
  if (existsSync(codexPath)) {
    const raw = JSON.parse(readFileSync(codexPath, 'utf8'));
    if (raw.hooks?.PreToolUse) {
      raw.hooks.PreToolUse = raw.hooks.PreToolUse.filter(
        group => !group.matcher?.includes('mcp__linear__')
      );
      writeFileSync(codexPath, JSON.stringify(raw, null, 2) + '\n', { mode: 0o600 });
    }
  }

  const vibePath = join(projectRoot, '.vibe', 'hooks.toml');
  if (existsSync(vibePath)) {
    const content = readFileSync(vibePath, 'utf8');
    const blocks = content.split(/(?=\[\[hooks\]\])/);
    const filtered = blocks.filter(block =>
      !block.includes('plain-english-issue') && !block.includes('_save_(issue|comment)')
    );
    writeFileSync(vibePath, filtered.join('').replace(/\n{3,}/g, '\n\n'), { mode: 0o600 });
  }
}

// --- Qwen prompt hook merge -------------------------------------------------------
// Qwen supports prompt hooks natively. Their text is owned by plain-english
// (the docs and github judges) and by the local settings (verify reminder,
// compaction memory), so the installer copies them from .claude/settings.json
// and .claude/settings.local.json at render time instead of duplicating them
// in the template. Claude-only fields (model pin, continueOnBlock, if) are
// dropped: a Qwen prompt hook runs on the session model.

function collectClaudePromptHooks() {
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
    const events = raw.hooks ?? {};
    for (const [event, groups] of Object.entries(events)) {
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

function mergeQwenPromptHooks(qwenSettings) {
  const prompts = collectClaudePromptHooks();
  let merged = 0;
  for (const entry of prompts) {
    const target = qwenTargetFor(entry);
    if (!target) continue;
    const [event, matcher] = target;
    const groups = qwenSettings.hooks[event] ?? [];
    let group = groups.find(g => (g.matcher ?? null) === matcher);
    if (!group) {
      group = matcher ? { matcher, hooks: [] } : { hooks: [] };
      groups.push(group);
      qwenSettings.hooks[event] = groups;
    }
    group.hooks.push(qwenPromptHook(entry.hook));
    merged += 1;
  }
  return merged;
}

// --- Main -----------------------------------------------------------------------

async function main() {
  console.log(`Discord Ferry agent-compat installer`);
  console.log(`Project root: ${projectRoot}\n`);

  // 1. Validate templates exist
  const requiredTemplates = [
    'codex-config.toml', 'codex-hooks.json',
    'vibe-hooks.toml', 'vibe-mcp.toml',
    'qwen-settings.json',
  ];
  for (const t of requiredTemplates) {
    if (!existsSync(join(templateDir, t))) {
      console.error(`Missing template: config/agent-compat/${t}`);
      process.exit(1);
    }
  }

  // 2. Validate skill topology
  const skillPlan = buildSkillPlan(projectRoot);
  if (skillPlan.errors.length > 0) {
    console.error('Skill topology errors:');
    for (const e of skillPlan.errors) console.error(`  ${e}`);
    process.exit(1);
  }

  // 3. Generate .codex/
  console.log('Generating .codex/ ...');
  const codexDir = join(projectRoot, '.codex');
  const codexAgentsDir = join(codexDir, 'agents');
  mkdirSync(codexAgentsDir, { recursive: true });

  const postToolMatcher = codexPostToolMatcher();
  writeFileSync(join(codexDir, 'config.toml'), render('codex-config.toml'), { mode: 0o600 });
  writeFileSync(join(codexDir, 'hooks.json'), render('codex-hooks.json', {
    '__POST_TOOL_MATCHER__': postToolMatcher,
  }), { mode: 0o600 });

  const roleFiles = ['coordinator.toml', 'reviewer.toml', 'explorer.toml', 'locator.toml'];
  const agentTemplateDir = join(templateDir, 'agents');
  for (const role of roleFiles) {
    const templatePath = join(agentTemplateDir, role);
    if (existsSync(templatePath)) {
      writeFileSync(join(codexAgentsDir, role), readFileSync(templatePath), { mode: 0o600 });
    }
  }
  reconcileDirectory(codexAgentsDir, roleFiles);
  console.log('  config.toml, hooks.json, agents/*.toml');

  // 4. Generate .vibe/
  console.log('Generating .vibe/ ...');
  const vibeDir = join(projectRoot, '.vibe');
  mkdirSync(vibeDir, { recursive: true });

  const vibePostMatcher = vibePostToolMatcher();
  writeFileSync(join(vibeDir, 'hooks.toml'), render('vibe-hooks.toml', {
    '__VIBE_POST_TOOL_MATCHER__': vibePostMatcher,
  }), { mode: 0o600 });
  // Vibe reads MCP servers from .vibe/config.toml, not a separate mcp.toml.
  // Older installer runs wrote mcp.toml, which Vibe ignores; remove the stale file.
  writeFileSync(join(vibeDir, 'config.toml'), render('vibe-mcp.toml'), { mode: 0o600 });
  const legacyMcp = join(vibeDir, 'mcp.toml');
  if (existsSync(legacyMcp)) {
    unlinkSync(legacyMcp);
    console.log('  removed legacy: mcp.toml');
  }
  console.log('  hooks.toml, config.toml (mcp servers)');

  // 4a. Generate .qwen/
  // Qwen speaks the Claude hook envelope, so the template registers the guard
  // scripts directly instead of an adapter. The prompt hooks are merged below
  // from the Claude settings; plain-english has no qwen agent profile to run
  // its own init against (ADR-026).
  console.log('Generating .qwen/ ...');
  const qwenDir = join(projectRoot, '.qwen');
  mkdirSync(qwenDir, { recursive: true });

  const qwenSettings = JSON.parse(render('qwen-settings.json', {
    '__QWEN_POST_TOOL_MATCHER__': qwenPostToolMatcher(),
  }));
  const mergedPrompts = mergeQwenPromptHooks(qwenSettings);
  writeFileSync(join(qwenDir, 'settings.json'), JSON.stringify(qwenSettings, null, 2) + '\n', { mode: 0o600 });
  reconcileDirectory(qwenDir, ['settings.json']);
  if (mergedPrompts > 0) {
    console.log(`  settings.json (${mergedPrompts} prompt hooks merged from .claude/)`);
  } else {
    console.log('  settings.json (no prompt hooks found in .claude/ to merge)');
  }

  // 4b. Merge plain-english lint hooks into both hook files.
  // The ferry templates carry only the adapter hooks. plain-english owns its own
  // blocks and merges them in, preserving the ferry hooks. This avoids duplicating
  // plain-english config in the templates (ADR-024). Qwen is not in this step:
  // plain-english has no qwen agent profile, so its shims are registered by the
  // template and its judges arrive through the prompt merge above.
  console.log('Merging plain-english lint hooks ...');
  if (plainEnglishAvailable()) {
    if (!existsSync(join(projectRoot, 'AGENTS.md'))) {
      // plain-english init creates a stub AGENTS.md when the file is missing,
      // which would mask the real instruction contract. Refuse rather than
      // generate a stub; the instruction layer comes from claude-setup.
      console.log('  AGENTS.md missing: skipping plain-english merge so no stub is created.');
      console.log('  Restore the instruction layer from claude-setup, then re-run.');
    } else {
      runPlainEnglishInit('codex');
      runPlainEnglishInit('vibe');
      stripIssueChannel();
      console.log('  stripped issue channel (Linear MCP not used in this repo)');
    }
  } else {
    console.log('  plain-english not found; skipping lint hook merge');
    console.log('  Install it (npm install -g plain-english) and re-run to get lint hooks');
  }

  // 5. Bridge skills
  console.log('Bridging skills ...');
  const bridged = applySkillPlan(projectRoot, skillPlan);
  const totalSkills = skillPlan.records.filter(r => r.type === 'claude-owned').length;
  console.log(`  ${totalSkills} skills, ${bridged} operations applied`);

  // 6. Summary
  console.log('\nDone. Generated files are gitignored.\n');
  console.log('Manual steps remaining:');
  console.log('');
  console.log('  Codex:');
  console.log('    1. Add a trust entry in ~/.codex/config.toml:');
  console.log(`       [projects."${projectRoot}"]`);
  console.log('       trust_level = "trusted"');
  console.log('    2. Enable multi-agent (for role files):');
  console.log('       [features]');
  console.log('       multi_agent = true');
  console.log('    3. Restart Codex CLI');
  console.log('');
  console.log('  Vibe:');
  console.log('    1. Set MISTRAL_API_KEY (env var or ~/.vibe/config.toml)');
  console.log('    2. Set the active model in ~/.vibe/config.toml (active_model = "<model>")');
  console.log('    3. Restart Vibe CLI');
  console.log('');
  console.log('  Qwen:');
  console.log('    1. Add MISTRAL_API_KEY to the env block of ~/.qwen/settings.json');
  console.log('       so the second-opinion MCP registers get_mistral_opinion');
  console.log('    2. Restart Qwen Code');
  console.log('');
  console.log('  Verify: ./scripts/agent-check.sh');
}

main().catch(err => {
  console.error(err);
  process.exit(1);
});
