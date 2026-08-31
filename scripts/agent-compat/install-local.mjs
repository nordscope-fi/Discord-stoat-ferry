#!/usr/bin/env node
// Discord Ferry — Cross-platform agent config installer.
// Renders templates from config/agent-compat/ into .codex/, .vibe/ and .qwen/,
// creates .agents/skills/ symlinks via the skill topology bridger.
// Run: ./scripts/agent-install.sh (or: node scripts/agent-compat/install-local.mjs)

import { execFileSync } from 'node:child_process';
import { existsSync, lstatSync, mkdirSync, readdirSync, readFileSync, statSync, unlinkSync, writeFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { codexPostToolMatcher, vibePostToolMatcher } from './hook-parity.mjs';
import {
  canonicalCheckoutRoot,
  normalizeCodexChatHooks,
  removeUnusedIssueChannel,
  requirePlainEnglish,
  stripVibeIssueChannel,
} from './plain-english-contract.mjs';
import { buildQwenSettings } from './qwen-settings-build.mjs';
import { buildSkillPlan, applySkillPlan } from './skill-topology.mjs';

const sourceRoot = resolve(execFileSync(
  'git', ['rev-parse', '--show-toplevel'], { encoding: 'utf8' },
).trim());
const projectRoot = canonicalCheckoutRoot(sourceRoot);
const templateDir = join(sourceRoot, 'config', 'agent-compat');
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

function hostDirIsLinked(path) {
  return lstatSync(path, { throwIfNoEntry: false })?.isSymbolicLink() ?? false;
}

function runPlainEnglishInit(agent) {
  try {
    execFileSync('plain-english', ['init', `--agent`, agent, '--root', projectRoot], {
      encoding: 'utf8',
      stdio: 'pipe',
    });
    console.log(`  plain-english init --agent ${agent}: merged lint hooks`);
  } catch (err) {
    throw new Error(
      `plain-english init --agent ${agent} failed; install it and re-run ` +
      `./scripts/agent-install.sh (${err.message})`,
    );
  }
}

// --- Main -----------------------------------------------------------------------

async function main() {
  console.log(`Discord Ferry agent-compat installer`);
  console.log(`Project root: ${projectRoot}\n`);
  if (sourceRoot !== projectRoot) console.log(`Templates: ${sourceRoot}\n`);

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
  const agentsDir = join(projectRoot, '.agents');
  const agentsLinked = hostDirIsLinked(agentsDir);
  if (!existsSync(join(projectRoot, 'AGENTS.md'))) {
    throw new Error('AGENTS.md is required before plain-English hooks can be generated');
  }
  requirePlainEnglish();

  // 3. Generate .codex/
  console.log('Generating .codex/ ...');
  const codexDir = join(projectRoot, '.codex');
  const codexLinked = hostDirIsLinked(codexDir);
  if (codexLinked) {
    console.log('  .codex/ is linked to another checkout; skipped (the canonical root owns it)');
  } else {
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
    const staleChatWrapper = join(codexDir, 'bin', 'plain-english-chat-hook.mjs');
    if (existsSync(staleChatWrapper)) {
      unlinkSync(staleChatWrapper);
      console.log('  removed replaced: bin/plain-english-chat-hook.mjs');
    }
    console.log('  config.toml, hooks.json, agents/*.toml');
  }

  // 4. Generate .vibe/
  console.log('Generating .vibe/ ...');
  const vibeDir = join(projectRoot, '.vibe');
  const vibeLinked = hostDirIsLinked(vibeDir);
  if (vibeLinked) {
    console.log('  .vibe/ is linked to another checkout; skipped (the canonical root owns it)');
  } else {
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
  }

  // 4a. Generate .qwen/
  // Qwen speaks the Claude hook envelope, so the template registers the guard
  // scripts directly instead of an adapter. The prompt hooks are merged below
  // from the Claude settings; plain-english has no qwen agent profile to run
  // its own init against (ADR-026).
  console.log('Generating .qwen/ ...');
  const qwenDir = join(projectRoot, '.qwen');

  // Worktrees link .qwen/ back to the checkout that owns it. Writing through
  // the link would embed this worktree's root in settings the canonical root
  // then fails to match, so the owner keeps generating and the link follows.
  const qwenLinked = hostDirIsLinked(qwenDir);
  if (qwenLinked) {
    console.log(`  .qwen/ is linked to another checkout; skipped (the canonical root owns it)`);
  } else {
    mkdirSync(qwenDir, { recursive: true });

    // The builder is shared with the drift checker, so install and check
    // agree by construction.
    const qwenSettings = buildQwenSettings({ projectRoot, home, templateDir });
    const promptCount = Object.values(qwenSettings.hooks ?? {})
      .flat()
      .flatMap(g => g.hooks ?? [])
      .filter(h => h.type === 'prompt').length;
    writeFileSync(join(qwenDir, 'settings.json'), JSON.stringify(qwenSettings, null, 2) + '\n', { mode: 0o600 });
    reconcileDirectory(qwenDir, ['settings.json']);
    if (promptCount > 0) {
      console.log(`  settings.json (${promptCount} prompt hooks merged from .claude/)`);
    } else {
      console.log('  settings.json (no prompt hooks found in .claude/ to merge)');
    }
    console.log('  Qwen review credentials are retrieved from Proton by qwen-review.mjs');
  }

  // 4b. Merge plain-english lint hooks into both hook files.
  // The ferry templates carry only the adapter hooks. plain-english owns its own
  // blocks and merges them in, preserving the ferry hooks. This avoids duplicating
  // plain-english config in the templates (ADR-024). Qwen is not in this step:
  // plain-english has no qwen agent profile, so its shims are registered by the
  // template and its judges arrive through the prompt merge above.
  console.log('Merging plain-english lint hooks ...');
  if (!codexLinked) runPlainEnglishInit('codex');
  if (!vibeLinked) runPlainEnglishInit('vibe');
  if (!vibeLinked) {
    const vibeHooksPath = join(projectRoot, '.vibe', 'hooks.toml');
    const content = readFileSync(vibeHooksPath, 'utf8');
    writeFileSync(vibeHooksPath, stripVibeIssueChannel(content), { mode: 0o600 });
  }
  if (!codexLinked) {
    const codexHooksPath = join(projectRoot, '.codex', 'hooks.json');
    const codexHooks = JSON.parse(readFileSync(codexHooksPath, 'utf8'));
    removeUnusedIssueChannel(codexHooks);
    normalizeCodexChatHooks(codexHooks, projectRoot);
    writeFileSync(codexHooksPath, `${JSON.stringify(codexHooks, null, 2)}\n`, { mode: 0o600 });
  }
  console.log('  installed native plain-English launchers and preserved linked host owners');

  // 5. Bridge skills
  console.log('Bridging skills ...');
  const totalSkills = skillPlan.records.filter(r => r.type === 'claude-owned').length;
  if (agentsLinked) {
    console.log('  .agents/ is linked to another checkout; skipped (the canonical root owns it)');
  } else {
    const bridged = applySkillPlan(projectRoot, skillPlan);
    console.log(`  ${totalSkills} skills, ${bridged} operations applied`);
  }

  // 6. Summary
  console.log('\nDone. Generated files are gitignored.\n');
  console.log('Machine-wide trust and reviewer access are managed by scripts/codex-setup.sh.');
  console.log('Run scripts/codex-setup.sh --live when paid runtime probes are required.');
}

main().catch(err => {
  console.error(err);
  process.exit(1);
});
