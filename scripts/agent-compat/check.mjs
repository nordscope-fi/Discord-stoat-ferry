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
  vibePostToolMatcher,
} from './hook-parity.mjs';
import {
  canonicalCheckoutRoot,
  codexChatCommand,
  normalizeCodexChatHooks,
  removeUnusedIssueChannel,
  requirePlainEnglish,
  stripVibeIssueChannel,
} from './plain-english-contract.mjs';
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
const worktreeAgentsPath = worktreeIndex === -1 ? null : args[worktreeIndex + 3];
const worktreeClaudePath = worktreeIndex === -1 ? null : args[worktreeIndex + 4];
const worktreeShipSkillPath = worktreeIndex === -1 ? null : args[worktreeIndex + 5];

const failures = [];
const warnings = [];

function fail(msg) { failures.push(msg); }
function warn(msg) { warnings.push(msg); }

if (focusedIndex !== -1 && !focusedHookPath) {
  fail('--check-codex-hooks requires a path');
}
if (worktreeIndex !== -1 && [
  worktreeScriptPath,
  worktreeIncludePath,
  worktreeAgentsPath,
  worktreeClaudePath,
  worktreeShipSkillPath,
].some(path => !path || path.startsWith('--'))) {
  fail(
    '--check-worktree-contract requires script, include, AGENTS.md, CLAUDE.md, and ship skill paths',
  );
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

export function canonicalJson(value) {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`;
  if (value && typeof value === 'object') {
    return `{${Object.keys(value).sort().map((key) =>
      `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(',')}}`;
  }
  return JSON.stringify(value);
}

function fileMode(path) {
  return statSync(path).mode & 0o777;
}

function sameFile(stagePath, actualPath) {
  return existsSync(actualPath) &&
    readFileSync(stagePath).equals(readFileSync(actualPath)) &&
    fileMode(stagePath) === fileMode(actualPath);
}

export function runPlainEnglishInit(
  stage,
  agent,
  { run = spawnSync, timeoutMs = 30_000 } = {},
) {
  const result = run(
    'plain-english', ['init', '--agent', agent, '--root', stage],
    { encoding: 'utf8', stdio: 'pipe', timeout: timeoutMs, killSignal: 'SIGTERM' },
  );
  if (result.error?.code === 'ETIMEDOUT') {
    fail(`plain-English ${agent} init exceeded ${timeoutMs}ms`);
    return false;
  }
  if (result.error || result.status !== 0) {
    const detail = result.stderr || result.error?.message || `exit ${result.status}`;
    fail(`plain-English ${agent} init failed: ${detail}`);
    return false;
  }
  return true;
}

function renderStagedFerryHosts(stage) {
  mkdirSync(join(stage, '.codex'), { recursive: true });
  mkdirSync(join(stage, '.vibe'), { recursive: true });
  writeFileSync(
    join(stage, '.codex', 'hooks.json'),
    render('codex-hooks.json', { '__POST_TOOL_MATCHER__': codexPostToolMatcher() }),
    { mode: 0o600 },
  );
  writeFileSync(
    join(stage, '.vibe', 'hooks.toml'),
    render('vibe-hooks.toml', { '__VIBE_POST_TOOL_MATCHER__': vibePostToolMatcher() }),
    { mode: 0o600 },
  );
}

function normalizeStagedHosts(stage, ownerRoot) {
  const codexPath = join(stage, '.codex', 'hooks.json');
  const codex = JSON.parse(readFileSync(codexPath, 'utf8'));
  removeUnusedIssueChannel(codex);
  normalizeCodexChatHooks(codex, ownerRoot);
  writeFileSync(codexPath, `${JSON.stringify(codex, null, 2)}\n`, { mode: 0o600 });

  const vibePath = join(stage, '.vibe', 'hooks.toml');
  writeFileSync(vibePath, stripVibeIssueChannel(readFileSync(vibePath, 'utf8')), {
    mode: 0o600,
  });
}

function validActualCodexChatHooks(document, ownerRoot) {
  const expectedCommand = codexChatCommand(ownerRoot);
  for (const event of ['Stop', 'SubagentStop']) {
    const matches = (document.hooks?.[event] ?? [])
      .flatMap((group) => group.hooks ?? [])
      .filter((hook) => hook.command === expectedCommand);
    if (matches.length !== 1) {
      fail(`expected one native plain-English chat hook for ${event}; found ${matches.length}`);
      return false;
    }
    if (matches[0].timeout !== 60) {
      fail(`unexpected native plain-English chat timeout for ${event}: ${matches[0].timeout}`);
      return false;
    }
  }
  return true;
}

function compareCodexArtifacts(stage, actualHooksPath) {
  const actualRoot = dirname(dirname(realpathSync(actualHooksPath)));
  const actual = JSON.parse(readFileSync(actualHooksPath, 'utf8'));
  if (!validActualCodexChatHooks(actual, actualRoot)) return;
  const expected = JSON.parse(readFileSync(join(stage, '.codex', 'hooks.json'), 'utf8'));
  if (canonicalJson(actual) !== canonicalJson(expected)) {
    fail('Codex hooks differ from staged plain-English output');
    return;
  }
  const expectedLauncher = join(stage, '.codex', 'hooks', 'plain-english.mjs');
  const actualLauncher = join(actualRoot, '.codex', 'hooks', 'plain-english.mjs');
  if (!existsSync(actualLauncher)) {
    fail('missing native Codex plain-English launcher');
  } else if (!sameFile(expectedLauncher, actualLauncher)) {
    fail('Codex plain-English launcher differs from staged output');
  }
  if (existsSync(join(actualRoot, '.codex', 'bin', 'plain-english-chat-hook.mjs'))) {
    fail('stale Ferry plain-English wrapper remains');
  }
}

function compareVibeArtifacts(stage, actualRoot = projectRoot) {
  const expectedHooks = join(stage, '.vibe', 'hooks.toml');
  const actualHooks = join(actualRoot, '.vibe', 'hooks.toml');
  if (!sameFile(expectedHooks, actualHooks)) {
    fail('Vibe hooks differ from staged plain-English output');
    return;
  }
  const expectedDir = join(stage, '.vibe', 'hooks');
  const actualDir = join(actualRoot, '.vibe', 'hooks');
  const expectedNames = readdirSync(expectedDir)
    .filter((name) => name.startsWith('plain-english'))
    .sort();
  const actualNames = existsSync(actualDir)
    ? readdirSync(actualDir).filter((name) => name.startsWith('plain-english')).sort()
    : [];
  for (const name of expectedNames) {
    if (!actualNames.includes(name)) {
      fail(`missing Vibe plain-English artifact: ${name}`);
      return;
    }
    if (!sameFile(join(expectedDir, name), join(actualDir, name))) {
      fail(`Vibe plain-English artifact differs: ${name}`);
      return;
    }
  }
  const unexpected = actualNames.filter((name) => !expectedNames.includes(name));
  if (unexpected.length > 0) {
    fail(`unexpected Vibe plain-English artifact: ${unexpected[0]}`);
  }
}

export function checkPlainEnglishState({
  codexHooksPath = join(projectRoot, '.codex', 'hooks.json'),
  includeVibe = true,
  stageParent = tmpdir(),
  runInit = runPlainEnglishInit,
  compareCodex = compareCodexArtifacts,
  compareVibe = compareVibeArtifacts,
  instructions = null,
} = {}) {
  const stage = mkdtempSync(join(stageParent, 'ferry-plain-english-'));
  try {
    const ownerRoot = dirname(dirname(realpathSync(codexHooksPath)));
    execFileSync('git', ['init', '-q', stage], { stdio: 'pipe' });
    writeFileSync(
      join(stage, 'AGENTS.md'),
      instructions ?? readFileSync(join(projectRoot, 'AGENTS.md')),
    );
    renderStagedFerryHosts(stage);
    if (!runInit(stage, 'codex', { timeoutMs: 30_000 })) return false;
    if (includeVibe && !runInit(stage, 'vibe', { timeoutMs: 30_000 })) return false;
    try {
      normalizeStagedHosts(stage, ownerRoot);
    } catch (err) {
      fail(`plain-English staged host normalization failed: ${err.message}`);
      return false;
    }
    compareCodex(stage, codexHooksPath);
    if (includeVibe) compareVibe(stage, dirname(dirname(codexHooksPath)));
    return true;
  } finally {
    rmSync(stage, { recursive: true, force: true });
  }
}

export function checkCodexHooks(actualPath) {
  return checkPlainEnglishState({ codexHooksPath: actualPath, includeVibe: false });
}

export function checkWorktreeContract(
  scriptPath,
  includePath,
  agentsPath,
  claudePath,
  shipSkillPath,
) {
  let script;
  let entries;
  let agents;
  let claude;
  let shipSkill;
  try {
    script = readFileSync(scriptPath, 'utf8');
    entries = new Set(readFileSync(includePath, 'utf8').split(/\r?\n/u).filter(Boolean));
    agents = readFileSync(agentsPath, 'utf8');
    claude = readFileSync(claudePath, 'utf8');
    shipSkill = readFileSync(shipSkillPath, 'utf8');
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

  const localManifest = 'docs/plans/change-manifest.md';
  const sharedManifest = '.claude/change-manifest.md';
  for (const [name, source] of [['AGENTS.md', agents], ['CLAUDE.md', claude]]) {
    if (!source.includes('mkdir -p docs/plans') || !source.includes(localManifest)
        || source.includes(sharedManifest)) {
      fail(`worktree manifest writer contract is invalid in ${name}`);
    }
  }
  const shipRequirements = [
    `head -1 ${localManifest}`,
    'If the manifest names this branch or this change',
    'If no manifest exists',
    `If \`${localManifest}\` exists`,
  ];
  if (shipRequirements.some((line) => !shipSkill.includes(line))
      || shipSkill.includes(sharedManifest)) {
    fail('worktree manifest reader contract is invalid in df-ship');
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
  const plainEnglishRequired = worktreeIndex === -1 && (focusedIndex !== -1 || !ci);
  if (failures.length === 0 && plainEnglishRequired) {
    try {
      requirePlainEnglish();
    } catch (err) {
      fail(err.message);
    }
  }

  if (failures.length > 0) {
    // Argument and prerequisite failures take precedence over generated-state work.
  } else if (worktreeIndex !== -1) {
    if (worktreeScriptPath && worktreeIncludePath && worktreeAgentsPath
        && worktreeClaudePath && worktreeShipSkillPath) {
      checkWorktreeContract(
        resolve(worktreeScriptPath),
        resolve(worktreeIncludePath),
        resolve(worktreeAgentsPath),
        resolve(worktreeClaudePath),
        resolve(worktreeShipSkillPath),
      );
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
    checkPlainEnglishState();
    checkQwenState();
    checkSkills();
    const snapshotRoot = canonicalCheckoutRoot(projectRoot);
    checkWorktreeContract(
      join(snapshotRoot, '.claude', 'scripts', 'new-worktree.sh'),
      join(snapshotRoot, '.worktreeinclude'),
      join(snapshotRoot, 'AGENTS.md'),
      join(snapshotRoot, 'CLAUDE.md'),
      join(snapshotRoot, '.claude', 'skills', 'df-ship', 'SKILL.md'),
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
