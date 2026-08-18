#!/usr/bin/env node
// Discord Ferry — Cross-platform agent config installer.
// Renders templates from config/agent-compat/ into .codex/ and .vibe/,
// creates .agents/skills/ symlinks via the skill topology bridger.
// Run: ./scripts/agent-install.sh (or: node scripts/agent-compat/install-local.mjs)

import { execFileSync } from 'node:child_process';
import { existsSync, mkdirSync, readdirSync, readFileSync, unlinkSync, writeFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { codexPostToolMatcher, vibePostToolMatcher } from './hook-parity.mjs';
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
      unlinkSync(fullPath);
      console.log(`  removed unexpected: ${name}`);
    }
  }
}

// --- Main -----------------------------------------------------------------------

async function main() {
  console.log(`Discord Ferry agent-compat installer`);
  console.log(`Project root: ${projectRoot}\n`);

  // 1. Validate templates exist
  const requiredTemplates = [
    'codex-config.toml', 'codex-hooks.json',
    'vibe-hooks.toml', 'vibe-mcp.toml',
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
  writeFileSync(join(vibeDir, 'mcp.toml'), render('vibe-mcp.toml'), { mode: 0o600 });
  console.log('  hooks.toml, mcp.toml');

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
  console.log('    2. Set model in ~/.vibe/config.toml (e.g. codestral-latest)');
  console.log('    3. Restart Vibe CLI');
  console.log('');
  console.log('  Verify: ./scripts/agent-check.sh');
}

main().catch(err => {
  console.error(err);
  process.exit(1);
});
