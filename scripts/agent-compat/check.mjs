#!/usr/bin/env node
// Discord Ferry — Cross-platform agent config drift verifier.
// Validates: instructions exist, ferry blocks present, plain-english lint hooks synced,
// skills are bridged, hook parity holds, no unresolved placeholders in generated files.
// Run: ./scripts/agent-check.sh [--strict] [--generated-only] [--ci]
//   --ci: check only tracked templates and hook parity structure (no gitignored files, CI-safe)

import { execFileSync, spawnSync } from 'node:child_process';
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  readlinkSync,
  realpathSync,
  rmSync,
  statSync,
  writeFileSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import {
  auditHookParity,
  codexPostToolMatcher,
  HOOK_PARITY,
  validateHookParity,
} from './hook-parity.mjs';
import {
  canonicalCheckoutRoot,
  DIRECT_CHAT_COMMAND,
  removeUnusedIssueChannel,
  transformCodexChatHooks,
} from './plain-english-chat-hook.mjs';
import { buildQwenSettings } from './qwen-settings-build.mjs';
import { buildSkillPlan } from './skill-topology.mjs';

const sourceRoot = resolve(execFileSync(
  'git', ['rev-parse', '--show-toplevel'], { encoding: 'utf8' },
).trim());
const projectRoot = canonicalCheckoutRoot(sourceRoot);
const templateDir = join(sourceRoot, 'config', 'agent-compat');
const home = process.env.HOME;

const args = process.argv.slice(2);
const strict = args.includes('--strict');
const generatedOnly = args.includes('--generated-only');
const ci = args.includes('--ci');
const focusedIndex = args.indexOf('--check-codex-hooks');
const focusedHookArgument = focusedIndex === -1 ? null : args[focusedIndex + 1];
const focusedHookPath = focusedHookArgument?.startsWith('--') ? null : focusedHookArgument;
const worktreeIndex = args.indexOf('--check-worktree-contract');
const worktreeScriptPath = worktreeIndex === -1 ? null : args[worktreeIndex + 1];
const worktreeIncludePath = worktreeIndex === -1 ? null : args[worktreeIndex + 2];

const failures = [];
const warnings = [];

function fail(msg) { failures.push(msg); }
function warn(msg) { warnings.push(msg); }

if (focusedIndex !== -1 && !focusedHookPath) {
  fail('--check-codex-hooks requires a path');
}
if (worktreeIndex !== -1 && (!worktreeScriptPath || !worktreeIncludePath)) {
  fail('--check-worktree-contract requires a script and include path');
}

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

function ferryBlocksPresent(generatedPath, adapterScript) {
  if (!existsSync(generatedPath)) {
    fail(`missing: ${generatedPath}`);
    return;
  }
  const content = readFileSync(generatedPath, 'utf8');
  if (!content.includes(adapterScript)) {
    fail(`ferry blocks missing: ${generatedPath} does not contain ${adapterScript}. Re-run ./scripts/agent-install.sh`);
  }
}

function plainEnglishAvailable() {
  const result = spawnSync('plain-english', ['--version'], { encoding: 'utf8', stdio: 'pipe' });
  return result.status === 0;
}

function plainEnglishUpToDate(agent) {
  if (!plainEnglishAvailable()) {
    warn(`plain-english not installed; skipping lint-hook sync check for ${agent}`);
    return;
  }
  let output;
  try {
    output = execFileSync('plain-english', ['init', '--agent', agent, '--dry-run', '--root', projectRoot], {
      encoding: 'utf8',
      stdio: 'pipe',
    });
  } catch (err) {
    warn(`plain-english init --dry-run failed for ${agent}: ${err.message}`);
    return;
  }
  const lines = output.split('\n');
  const pending = lines.filter(line => {
    if (line.includes('Nothing was written') || line.includes('After installing')) return false;
    if (/added:\s+(?!none\b)/.test(line)) {
      return !line.includes('mcp__linear__') && !line.includes('_save_(issue|comment)');
    }
    const createMatch = line.match(/^\s+create\s+(.+)/);
    if (createMatch) {
      const filePath = createMatch[1].trim();
      return !existsSync(join(projectRoot, filePath));
    }
    if (/^\s+delete\s/.test(line)) return true;
    return false;
  });
  if (pending.length > 0) {
    fail(`plain-english lint hooks out of sync for ${agent}. Re-run ./scripts/agent-install.sh:\n${pending.map(l => l.trim()).join('\n')}`);
  }
}

export function canonicalJson(value) {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`;
  if (value && typeof value === 'object') {
    return `{${Object.keys(value).sort().map((key) =>
      `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(',')}}`;
  }
  return JSON.stringify(value);
}

export function assertTwoChatWrappers(document) {
  const all = ['Stop', 'SubagentStop'].flatMap((event) =>
    (document.hooks?.[event] ?? []).flatMap((group) => group.hooks ?? []));
  const wrappers = all.filter((hook) => hook.command?.includes('plain-english-chat-hook.mjs'));
  const direct = all.filter((hook) => hook.command === DIRECT_CHAT_COMMAND);
  if (wrappers.length !== 2 || wrappers.some((hook) => hook.timeout !== 60) || direct.length) {
    fail('expected exactly two 60-second Ferry chat wrappers and no direct chat hook');
  }
}

export function checkCodexHooks(actualPath) {
  const stage = mkdtempSync(join(tmpdir(), 'ferry-codex-hooks-'));
  try {
    mkdirSync(join(stage, '.codex'), { recursive: true });
    writeFileSync(join(stage, 'AGENTS.md'), readFileSync(join(projectRoot, 'AGENTS.md')));
    writeFileSync(
      join(stage, '.codex', 'hooks.json'),
      render('codex-hooks.json', { '__POST_TOOL_MATCHER__': codexPostToolMatcher() }),
    );
    try {
      execFileSync('plain-english', ['init', '--agent', 'codex', '--root', stage], {
        stdio: 'pipe',
      });
    } catch (err) {
      fail(`plain-English prerequisite failed during staged Codex hook generation: ${err.message}`);
      return;
    }
    const expected = JSON.parse(readFileSync(join(stage, '.codex', 'hooks.json'), 'utf8'));
    removeUnusedIssueChannel(expected);
    try {
      transformCodexChatHooks(expected, canonicalCheckoutRoot(projectRoot));
    } catch (err) {
      fail(`Codex chat-hook transform failed: ${err.message}`);
      return;
    }
    const actual = JSON.parse(readFileSync(actualPath, 'utf8'));
    assertTwoChatWrappers(actual);
    if (canonicalJson(actual) !== canonicalJson(expected)) {
      fail('Codex hooks differ from staged plain-English plus Ferry transformation');
    }
  } finally {
    rmSync(stage, { recursive: true, force: true });
  }
}

export function checkWorktreeContract(scriptPath, includePath) {
  let script;
  let entries;
  try {
    script = readFileSync(scriptPath, 'utf8');
    entries = new Set(readFileSync(includePath, 'utf8').split(/\r?\n/u).filter(Boolean));
  } catch (err) {
    fail(`worktree contract could not be read: ${err.message}`);
    return;
  }
  const copiedHosts = ['.agents/**', '.codex/**', '.vibe/**', '.qwen/**']
    .filter((entry) => entries.has(entry));
  const required = [
    'for host_dir in .agents .codex .vibe .qwen; do',
    'ln -s "../../$host_dir" "$WT/$host_dir"',
    'for link in CLAUDE.md .claude/rules .claude/skills AGENTS.md .agents .codex .vibe .qwen; do',
  ];
  if (copiedHosts.length > 0 || required.some((line) => !script.includes(line))) {
    fail('worktree contract must use four canonical host links and copy none of their state');
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

  fileMatches(join(codexDir, 'config.toml'), render('codex-config.toml'));
  ferryBlocksPresent(join(codexDir, 'hooks.json'), 'codex-hook-adapter.mjs');
  checkCodexHooks(join(codexDir, 'hooks.json'));

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

  ferryBlocksPresent(join(vibeDir, 'hooks.toml'), 'vibe-hook-adapter.mjs');
  plainEnglishUpToDate('vibe');
  // Vibe reads MCP servers from .vibe/config.toml; the installer renders the
  // vibe-mcp.toml template there and removes any legacy mcp.toml.
  fileMatches(join(vibeDir, 'config.toml'), render('vibe-mcp.toml'));
}

// --- Check: Generated state (Qwen) ----------------------------------------------
// .qwen/settings.json is fully generator-owned (plain-english has no qwen
// profile that would merge into it), so the right strength here is exact
// equality with a fresh build from the shared builder. A guard moved under
// the wrong event, a changed matcher, or a stale prompt all fail.

function checkQwenState() {
  const settingsPath = join(projectRoot, '.qwen', 'settings.json');
  if (!existsSync(settingsPath)) {
    fail('.qwen/settings.json not found. Run ./scripts/agent-install.sh');
    return;
  }

  // Worktrees link .qwen/ back to the primary checkout (ADR-019 style), and
  // the generated settings embed the root they were rendered from. Rebuild
  // from the real root behind the link, not the session cwd, so a linked
  // worktree compares against the file that actually exists.
  const canonicalRoot = dirname(dirname(realpathSync(settingsPath)));
  let expected;
  try {
    expected = buildQwenSettings({
      projectRoot: canonicalRoot,
      home,
      templateDir,
    });
  } catch (err) {
    fail(`qwen template failed to render: ${err.message}`);
    return;
  }

  // Qwen maintains its own schema marker ($version) in the file and bumps it
  // on startup. That key belongs to the host, not the generator, so compare
  // without it instead of reporting drift after every launch.
  let actual;
  try {
    actual = JSON.parse(readFileSync(settingsPath, 'utf8'));
  } catch (err) {
    fail(`.qwen/settings.json is not valid JSON: ${err.message}`);
    return;
  }
  if (typeof actual !== 'object' || actual === null || Array.isArray(actual)) {
    fail('.qwen/settings.json is not a JSON object. Re-run ./scripts/agent-install.sh');
    return;
  }
  delete actual.$version;
  const actualCanonical = canonicalJson(actual);
  const expectedCanonical = canonicalJson(expected);
  if (actualCanonical !== expectedCanonical) {
    const limit = Math.min(actualCanonical.length, expectedCanonical.length);
    let firstDifference = limit;
    for (let index = 0; index < limit; index += 1) {
      if (actualCanonical[index] !== expectedCanonical[index]) {
        firstDifference = index;
        break;
      }
    }
    fail(
      'drift: .qwen/settings.json does not match the template plus merged prompt hooks ' +
      `(first difference ${firstDifference}; lengths ${actualCanonical.length}/` +
      `${expectedCanonical.length}). Re-run ./scripts/agent-install.sh`,
    );
  }
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
    const localPath = join(projectRoot, '.claude', 'settings.local.json');
    if (existsSync(localPath)) {
      const localRaw = JSON.parse(readFileSync(localPath, 'utf8'));
      const localHooks = localRaw.hooks ?? localRaw;
      if (projectSettings) {
        for (const [event, groups] of Object.entries(localHooks)) {
          if (!Array.isArray(groups)) continue;
          projectSettings[event] = [...(projectSettings[event] ?? []), ...groups];
        }
      } else {
        projectSettings = localHooks;
      }
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
    { host: 'codex', path: join(projectRoot, '.codex', 'config.toml') },
    { host: 'codex', path: join(projectRoot, '.codex', 'hooks.json') },
    { host: 'vibe', path: join(projectRoot, '.vibe', 'hooks.toml') },
    { host: 'vibe', path: join(projectRoot, '.vibe', 'config.toml') },
    { host: 'qwen', path: join(projectRoot, '.qwen', 'settings.json') },
  ];

  for (const { path: filePath } of generatedFiles) {
    if (!existsSync(filePath)) continue;
    const content = readFileSync(filePath, 'utf8');
    if (content.includes('__PROJECT_ROOT__') || content.includes('__HOME__') ||
        content.includes('__POST_TOOL_MATCHER__') || content.includes('__VIBE_POST_TOOL_MATCHER__')) {
      fail(`unresolved placeholder in ${filePath}`);
    }
  }
  const violations = generatedHostSecretViolations(generatedFiles
    .filter(({ path }) => existsSync(path))
    .map(({ host, path }) => ({ host, content: readFileSync(path, 'utf8') })));
  for (const host of violations) {
    fail(`generated ${host} state contains an inline credential. Run ./scripts/agent-install.sh`);
  }
}

const SECRET_SHAPE = /(?:sk-[A-Za-z0-9_-]{16,}|pst_[A-Za-z0-9_-]{32,}|AKIA[A-Z0-9]{16})/u;

export function generatedHostSecretViolations(files) {
  const violations = new Set();
  for (const { host, content } of files) {
    if (!['codex', 'vibe', 'qwen'].includes(host) || typeof content !== 'string') continue;
    if (SECRET_SHAPE.test(content)) violations.add(host);
    if (host === 'qwen') {
      try {
        const settings = JSON.parse(content);
        if (Object.hasOwn(settings, 'env')) violations.add(host);
      } catch {
        // The Qwen state validator reports malformed JSON separately.
      }
    }
  }
  return [...violations].sort();
}

// --- Check: Templates (CI mode) -------------------------------------------------

function checkTemplates() {
  const requiredTemplates = [
    'codex-config.toml', 'codex-hooks.json',
    'vibe-hooks.toml', 'vibe-mcp.toml',
    'qwen-settings.json',
  ];
  for (const t of requiredTemplates) {
    if (!existsSync(join(templateDir, t))) {
      fail(`missing template: config/agent-compat/${t}`);
    }
  }

  const roleFiles = ['coordinator.toml', 'reviewer.toml', 'explorer.toml', 'locator.toml'];
  for (const role of roleFiles) {
    if (!existsSync(join(templateDir, 'agents', role))) {
      fail(`missing template: config/agent-compat/agents/${role}`);
    }
  }

  const testReplacements = {
    '__POST_TOOL_MATCHER__': '^(test)$',
    '__VIBE_POST_TOOL_MATCHER__': 're:^(test)$',
    '__QWEN_POST_TOOL_MATCHER__': 'write_file|edit',
  };
  for (const t of requiredTemplates) {
    const rendered = render(t, testReplacements);
    if (rendered.includes('__') && /__[A-Z_]+__/.test(rendered)) {
      fail(`unresolved placeholder in template ${t}`);
    }
  }

  try {
    const codexHooks = render('codex-hooks.json', testReplacements);
    JSON.parse(codexHooks);
  } catch (err) {
    fail(`codex-hooks.json template is not valid JSON after rendering: ${err.message}`);
  }

  try {
    const qwenSettings = render('qwen-settings.json', testReplacements);
    JSON.parse(qwenSettings);
  } catch (err) {
    fail(`qwen-settings.json template is not valid JSON after rendering: ${err.message}`);
  }

  const parityIssues = validateHookParity(HOOK_PARITY);
  for (const issue of parityIssues) {
    fail(`hook parity structure: ${issue}`);
  }
}

// --- Main -----------------------------------------------------------------------

function main() {
  if (worktreeIndex !== -1) {
    if (worktreeScriptPath && worktreeIncludePath) {
      checkWorktreeContract(resolve(worktreeScriptPath), resolve(worktreeIncludePath));
    }
  } else if (focusedIndex !== -1) {
    if (focusedHookPath) checkCodexHooks(resolve(focusedHookPath));
  } else if (ci) {
    checkTemplates();
  } else {
    if (!generatedOnly) {
      checkInstructions();
    }

    checkCodexState();
    checkVibeState();
    checkQwenState();
    checkSkills();
    const snapshotRoot = canonicalCheckoutRoot(projectRoot);
    checkWorktreeContract(
      join(snapshotRoot, '.claude', 'scripts', 'new-worktree.sh'),
      join(snapshotRoot, '.worktreeinclude'),
    );

    if (!generatedOnly) {
      checkHookParity();
    }

    checkConfigSafety();
  }

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
}

let invokedAsMain = false;
if (process.argv[1]) {
  try {
    invokedAsMain = import.meta.url === pathToFileURL(realpathSync(process.argv[1])).href;
  } catch { /* an invalid entrypoint cannot be the current module */ }
}

if (invokedAsMain) {
  main();
}
