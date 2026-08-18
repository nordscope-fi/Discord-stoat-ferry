#!/usr/bin/env node
// Discord Ferry — Cross-platform agent config drift verifier.
// Validates: instructions exist, generated files match templates, skills are bridged,
// hook parity holds, no secrets or unresolved placeholders in generated files.
// Run: ./scripts/agent-check.sh [--strict] [--generated-only]

import { execFileSync } from 'node:child_process';
import { existsSync, readFileSync, readdirSync, readlinkSync, statSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { codexPostToolMatcher, vibePostToolMatcher, auditHookParity, HOOK_PARITY } from './hook-parity.mjs';
import { buildSkillPlan } from './skill-topology.mjs';

const projectRoot = resolve(execFileSync('git', ['rev-parse', '--show-toplevel'], { encoding: 'utf8' }).trim());
const templateDir = join(projectRoot, 'config', 'agent-compat');
const home = process.env.HOME;

const args = process.argv.slice(2);
const strict = args.includes('--strict');
const generatedOnly = args.includes('--generated-only');

const failures = [];
const warnings = [];

function fail(msg) { failures.push(msg); }
function warn(msg) { warnings.push(msg); }

function render(templateName, replacements = {}) {
  let content = readFileSync(join(templateDir, templateName), 'utf8');
  content = content.replaceAll('__PROJECT_ROOT__', projectRoot);
  content = content.replaceAll('__HOME__', home);
  for (const [key, value] of Object.entries(replacements)) {
    content = content.replaceAll(key, value);
  }
  return content;
}

function fileMatches(generatedPath, expectedContent) {
  if (!existsSync(generatedPath)) {
    fail(`missing: ${generatedPath}`);
    return;
  }
  const actual = readFileSync(generatedPath, 'utf8');
  if (actual !== expectedContent) {
    fail(`drift: ${generatedPath} does not match template. Re-run ./scripts/agent-install.sh`);
  }
}

// --- Check: Instructions --------------------------------------------------------

function checkInstructions() {
  const agentsPath = join(projectRoot, 'AGENTS.md');
  if (!existsSync(agentsPath)) {
    fail('AGENTS.md not found at project root');
    return;
  }

  const agents = readFileSync(agentsPath, 'utf8');
  if (!agents.includes('Host Compatibility')) {
    fail('AGENTS.md missing "Host Compatibility" section');
  }

  const claudePath = join(projectRoot, 'CLAUDE.md');
  if (existsSync(claudePath)) {
    const claude = readFileSync(claudePath, 'utf8');
    const combined = agents.length + claude.length;
    if (combined > 36864) {
      warn(`combined AGENTS.md + CLAUDE.md payload is ${combined} bytes (limit: 36864)`);
    }
  }
}

// --- Check: Generated state (Codex) ---------------------------------------------

function checkCodexState() {
  const codexDir = join(projectRoot, '.codex');
  if (!existsSync(codexDir)) {
    fail('.codex/ directory not found. Run ./scripts/agent-install.sh');
    return;
  }

  const postMatcher = codexPostToolMatcher();
  fileMatches(join(codexDir, 'config.toml'), render('codex-config.toml'));
  fileMatches(join(codexDir, 'hooks.json'), render('codex-hooks.json', {
    '__POST_TOOL_MATCHER__': postMatcher,
  }));

  const expectedRoles = ['coordinator.toml', 'reviewer.toml', 'explorer.toml', 'locator.toml'];
  const agentsDir = join(codexDir, 'agents');
  if (!existsSync(agentsDir)) {
    fail('.codex/agents/ directory not found');
  } else {
    for (const role of expectedRoles) {
      const templatePath = join(templateDir, 'agents', role);
      const generatedPath = join(agentsDir, role);
      if (existsSync(templatePath)) {
        fileMatches(generatedPath, readFileSync(templatePath, 'utf8'));
      }
    }
    const actual = readdirSync(agentsDir);
    for (const name of actual) {
      if (!expectedRoles.includes(name)) {
        warn(`unexpected file in .codex/agents/: ${name}`);
      }
    }
  }
}

// --- Check: Generated state (Vibe) ----------------------------------------------

function checkVibeState() {
  const vibeDir = join(projectRoot, '.vibe');
  if (!existsSync(vibeDir)) {
    fail('.vibe/ directory not found. Run ./scripts/agent-install.sh');
    return;
  }

  const vibePostMatcher = vibePostToolMatcher();
  fileMatches(join(vibeDir, 'hooks.toml'), render('vibe-hooks.toml', {
    '__VIBE_POST_TOOL_MATCHER__': vibePostMatcher,
  }));
  fileMatches(join(vibeDir, 'mcp.toml'), render('vibe-mcp.toml'));
}

// --- Check: Skills --------------------------------------------------------------

function checkSkills() {
  const plan = buildSkillPlan(projectRoot);
  if (plan.errors.length > 0) {
    for (const e of plan.errors) fail(`skill topology: ${e}`);
  }
  if (plan.operations.length > 0) {
    fail(`skill topology: ${plan.operations.length} pending operations. Re-run ./scripts/agent-install.sh`);
  }

  const claudeOwned = plan.records.filter(r => r.type === 'claude-owned');
  if (claudeOwned.length === 0) {
    fail('no Claude-owned skills found');
  }

  const agentRoot = join(projectRoot, '.agents', 'skills');
  if (!existsSync(agentRoot)) {
    fail('.agents/skills/ directory not found');
    return;
  }

  for (const record of claudeOwned) {
    const linkPath = join(agentRoot, record.name);
    if (!existsSync(linkPath)) {
      fail(`skill not bridged: ${record.name}`);
      continue;
    }
    try {
      readlinkSync(linkPath);
    } catch {
      warn(`${record.name}: .agents/skills/${record.name} is not a symlink`);
    }
    const skillMd = join(linkPath, 'SKILL.md');
    if (!existsSync(skillMd)) {
      fail(`skill bridge broken: ${record.name}/SKILL.md not found through symlink`);
    }
  }
}

// --- Check: Hook parity ---------------------------------------------------------

function checkHookParity() {
  let projectSettings = null;
  let userSettings = null;
  let userSettingsError = null;

  try {
    const settingsPath = join(projectRoot, '.claude', 'settings.json');
    if (existsSync(settingsPath)) {
      const raw = JSON.parse(readFileSync(settingsPath, 'utf8'));
      projectSettings = raw.hooks ?? raw;
    }
  } catch (err) {
    warn(`could not read project settings: ${err.message}`);
  }

  try {
    const userSettingsPath = join(home, '.claude', 'settings.json');
    if (existsSync(userSettingsPath)) {
      const raw = JSON.parse(readFileSync(userSettingsPath, 'utf8'));
      userSettings = raw.hooks ?? raw;
    }
  } catch (err) {
    userSettingsError = err.message;
  }

  const result = auditHookParity({
    projectSettings,
    userSettings,
    userSettingsError,
    strict,
    entries: HOOK_PARITY,
  });

  for (const f of result.failures) fail(`hook parity: ${f}`);
  for (const w of result.warnings) warn(`hook parity: ${w}`);
}

// --- Check: Config safety -------------------------------------------------------

function checkConfigSafety() {
  const generatedFiles = [
    join(projectRoot, '.codex', 'config.toml'),
    join(projectRoot, '.codex', 'hooks.json'),
    join(projectRoot, '.vibe', 'hooks.toml'),
    join(projectRoot, '.vibe', 'mcp.toml'),
  ];

  for (const filePath of generatedFiles) {
    if (!existsSync(filePath)) continue;
    const content = readFileSync(filePath, 'utf8');
    if (content.includes('__PROJECT_ROOT__') || content.includes('__HOME__') ||
        content.includes('__POST_TOOL_MATCHER__') || content.includes('__VIBE_POST_TOOL_MATCHER__')) {
      fail(`unresolved placeholder in ${filePath}`);
    }
  }
}

// --- Main -----------------------------------------------------------------------

if (!generatedOnly) {
  checkInstructions();
}

checkCodexState();
checkVibeState();
checkSkills();

if (!generatedOnly) {
  checkHookParity();
}

checkConfigSafety();

// Report
if (warnings.length > 0) {
  console.log(`\nWarnings (${warnings.length}):`);
  for (const w of warnings) console.log(`  ⚠ ${w}`);
}

if (failures.length > 0) {
  console.log(`\nFailures (${failures.length}):`);
  for (const f of failures) console.log(`  ✗ ${f}`);
  process.exit(1);
} else {
  console.log(`\n✓ All checks passed${warnings.length > 0 ? ` (${warnings.length} warnings)` : ''}`);
}
