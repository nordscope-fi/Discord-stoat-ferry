// Discord Ferry — Qwen readiness checker.
// Mirrors vibe-readiness.mjs structure for the Qwen Code host. Reuses shared
// checks (instructions, skills, worktree-parity, reviewer-clients,
// reviewer-runtime, context7-credential, generated-state) and adds
// Qwen-specific checks (version, config, trust, hooks, mcp-registration).
//
// Usage:
//   node qwen-readiness.mjs --root <path> [--home <path>] [--json]
//                          [--static] [--reviewers] [--self-test]

import { existsSync, readFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { execFileSync } from 'node:child_process';
import { canonicalCheckoutRoot } from './plain-english-contract.mjs';

// --- Shared helpers (mirrors vibe-readiness.mjs) ------------------------------

function readinessRecord(id, className, status, durationMs, remediation, details) {
  return {
    id,
    class: className,
    status,
    duration_ms: Math.max(0, Math.round(durationMs)),
    remediation: remediation ?? null,
    details: details ?? {},
  };
}

function safeReadinessFailure(id, className, reason, remediation, durationMs) {
  return readinessRecord(id, className, 'fail', durationMs, remediation, { reason });
}

async function checkRecord({ id, className, remediation, reason, now }, check) {
  const started = now();
  try {
    const details = await check();
    return readinessRecord(id, className, 'ok', now() - started, null, details ?? {});
  } catch (err) {
    const failure = safeReadinessFailure(id, className, reason, remediation, now() - started);
    failure.details.error = err instanceof Error ? err.message : String(err);
    return failure;
  }
}

const defaultNow = () => performance.now();

// --- Shared checks -------------------------------------------------------------

function hasReviewBoundary(text) {
  return text.includes('Host Compatibility')
    && text.includes('Tool name mapping');
}

async function checkInstructions(root) {
  const agents = join(root, 'AGENTS.md');
  if (!existsSync(agents)) throw new Error('AGENTS.md not found');
  const text = readFileSync(agents, 'utf8');
  if (!hasReviewBoundary(text)) throw new Error('review boundary text missing');
  return {};
}

async function checkSkills(root) {
  const skillsDir = join(root, '.claude', 'skills');
  const agentsDir = join(root, '.agents', 'skills');
  if (!existsSync(skillsDir)) throw new Error('.claude/skills/ not found');
  const skillNames = execFileSync('ls', [skillsDir], { encoding: 'utf8' })
    .split('\n').filter(Boolean);
  for (const name of skillNames) {
    const link = join(agentsDir, name);
    if (!existsSync(link)) throw new Error(`skill bridge missing: ${name}`);
  }
  return { skills: skillNames.length };
}

async function checkWorktreeParity(root) {
  // Mirrors codex-readiness: the contract check reads the canonical
  // checkout's instruction layer, because worktrees only link it.
  const ownedRoot = canonicalCheckoutRoot(root);
  try {
    execFileSync('node', [
      join(root, 'scripts', 'agent-compat', 'check.mjs'),
      '--check-worktree-contract',
      join(ownedRoot, '.claude', 'scripts', 'new-worktree.sh'),
      join(ownedRoot, '.worktreeinclude'),
      join(ownedRoot, 'AGENTS.md'),
      join(ownedRoot, 'CLAUDE.md'),
      join(ownedRoot, '.claude', 'skills', 'df-ship', 'SKILL.md'),
    ], { encoding: 'utf8', stdio: 'pipe' });
  } catch (err) {
    throw new Error(err.stdout || err.message);
  }
  return {};
}

async function checkReviewerClients() {
  const clients = ['vibe', 'qwen', 'claude'];
  const found = {};
  for (const cli of clients) {
    try {
      const version = execFileSync(cli, ['--version'], { encoding: 'utf8', stdio: 'pipe', timeout: 5000 });
      found[cli] = version.trim().split('\n')[0];
    } catch {
      throw new Error(`${cli} CLI not found`);
    }
  }
  return found;
}

async function checkReviewerRuntime(root, home) {
  const runtimeDir = join(home, '.local', 'share', 'discord-ferry', 'reviewer-runtime', 'current');
  if (!existsSync(runtimeDir)) throw new Error('reviewer runtime not installed');
  const manifestPath = join(runtimeDir, 'manifest.json');
  if (!existsSync(manifestPath)) throw new Error('manifest.json not found');
  return { path: runtimeDir };
}

async function checkContext7Credential(root, home) {
  const launcher = join(home, '.local', 'share', 'discord-ferry', 'reviewer-runtime', 'current', 'context7-mcp.mjs');
  if (!existsSync(launcher)) throw new Error('context7 launcher not found');
  // --check performs a live Proton Pass round-trip for the Context7 key. That
  // call is fast when warm but can exceed a minute when Proton is cold, so the
  // budget is generous rather than tight.
  try {
    const output = execFileSync('node', [launcher, '--check'], {
      encoding: 'utf8', stdio: 'pipe', timeout: 120000,
      env: { ...process.env, HOME: home },
    });
    if (!output.includes('context7 credential ready')) throw new Error('credential not ready');
  } catch (err) {
    throw new Error(err.stdout || err.message || 'context7 credential check failed');
  }
  return {};
}

async function checkGeneratedState(root) {
  try {
    execFileSync('node', [
      join(root, 'scripts', 'agent-compat', 'check.mjs'),
      '--strict',
      '--root', root,
    ], { encoding: 'utf8', stdio: 'pipe' });
  } catch (err) {
    throw new Error(err.stdout || err.message || 'generated-state drift detected');
  }
  return {};
}

// --- Qwen-specific checks -------------------------------------------------------

async function checkQwenVersion() {
  const version = execFileSync('qwen', ['--version'], { encoding: 'utf8', stdio: 'pipe', timeout: 5000 });
  return { version: version.trim().split('\n')[0] };
}

function readQwenSettings(root) {
  const configPath = join(root, '.qwen', 'settings.json');
  if (!existsSync(configPath)) throw new Error('.qwen/settings.json not found');
  try {
    return JSON.parse(readFileSync(configPath, 'utf8'));
  } catch (err) {
    throw new Error(`.qwen/settings.json does not parse: ${err.message}`);
  }
}

async function checkProjectConfig(root) {
  const settings = readQwenSettings(root);
  const servers = settings.mcpServers ?? {};
  for (const name of ['serena', 'qmd', 'context7']) {
    if (!servers[name]) throw new Error(`MCP server ${name} not registered`);
  }
  const hooks = settings.hooks ?? {};
  if (Object.keys(hooks).length === 0) throw new Error('hooks missing');
  const allow = settings.permissions?.allow;
  if (!Array.isArray(allow) || allow.length === 0) throw new Error('permissions allowlist empty');
  return { servers: Object.keys(servers).length };
}

async function checkQwenTrust(root, home) {
  // Qwen records folder trust in ~/.qwen/trustedFolders.json when the file
  // exists. This Qwen release also runs folders without that file (trust is
  // granted outside the file), so an absent file is reported, not failed.
  const trustPath = join(home, '.qwen', 'trustedFolders.json');
  if (!existsSync(trustPath)) return { fileBased: false };
  const text = readFileSync(trustPath, 'utf8');
  const canonical = resolve(root);
  if (!text.includes(canonical) && !text.includes(root)) {
    throw new Error(`project path not trusted: ${canonical}`);
  }
  return { path: canonical };
}

async function checkQwenHooks(root) {
  const settings = readQwenSettings(root);
  const hooks = settings.hooks;
  if (!hooks || typeof hooks !== 'object' || Object.keys(hooks).length === 0) {
    throw new Error('hooks missing');
  }
  const text = JSON.stringify(hooks);
  const requiredGuards = [
    'credential-guard.sh',
    'destructive-git-guard.mjs',
    'qwen-session-start.mjs',
    'qwen-stop-guard.mjs',
  ];
  for (const guard of requiredGuards) {
    if (!text.includes(guard)) throw new Error(`hook ${guard} not registered`);
  }
  return { hooks: requiredGuards.length };
}

async function checkMcpRegistration(root) {
  const settings = readQwenSettings(root);
  const servers = settings.mcpServers ?? {};
  const required = ['serena', 'qmd', 'context7'];
  for (const name of required) {
    if (!servers[name]) throw new Error(`MCP server ${name} not registered`);
  }
  return { servers: required };
}

// --- Static readiness -----------------------------------------------------------

export async function runStaticReadiness({ root, home, now = defaultNow } = {}) {
  const projectRoot = root ?? process.cwd();
  const homeDir = home ?? process.env.HOME;
  const records = [];

  const checks = [
    { id: 'qwen-version', className: 'runtime', reason: 'qwen CLI not found', remediation: 'Install Qwen Code: npm install -g @qwen-code/qwen-code', check: checkQwenVersion },
    { id: 'project-config', className: 'configuration', reason: '.qwen/settings.json missing required fields', remediation: 'Run ./scripts/agent-install.sh to regenerate', check: () => checkProjectConfig(projectRoot) },
    { id: 'qwen-trust', className: 'configuration', reason: 'project folder not trusted', remediation: `Trust the folder: run qwen in ${projectRoot} and accept the trust prompt`, check: () => checkQwenTrust(projectRoot, homeDir) },
    { id: 'instructions', className: 'instructions', reason: 'AGENTS.md review boundary text missing', remediation: 'Restore AGENTS.md from the repository', check: () => checkInstructions(projectRoot) },
    { id: 'skills', className: 'instructions', reason: 'skill bridge incomplete', remediation: 'Run ./scripts/agent-install.sh to rebuild skill symlinks', check: () => checkSkills(projectRoot) },
    { id: 'qwen-hooks', className: 'hooks', reason: '.qwen/settings.json missing required hooks', remediation: 'Run ./scripts/agent-install.sh to regenerate hooks', check: () => checkQwenHooks(projectRoot) },
    { id: 'mcp-registration', className: 'tool-servers', reason: 'required MCP servers not registered', remediation: 'Run ./scripts/agent-install.sh to restore MCP servers', check: () => checkMcpRegistration(projectRoot) },
    { id: 'worktree-parity', className: 'worktrees', reason: 'worktree link contract violated', remediation: 'Run .claude/scripts/new-worktree.sh to repair links', check: () => checkWorktreeParity(projectRoot) },
    { id: 'reviewer-clients', className: 'reviewers', reason: 'reviewer CLI not found', remediation: 'Install the missing CLI tool', check: checkReviewerClients },
    { id: 'reviewer-runtime', className: 'reviewers', reason: 'reviewer runtime not installed', remediation: 'Run scripts/codex-setup.sh to install the shared reviewer runtime', check: () => checkReviewerRuntime(projectRoot, homeDir) },
    { id: 'context7-credential', className: 'tool-servers', reason: 'context7 credential not available', remediation: 'Check Context7 API key provisioning', check: () => checkContext7Credential(projectRoot, homeDir) },
    { id: 'generated-state', className: 'configuration', reason: 'generated-state drift detected', remediation: 'Run ./scripts/agent-install.sh to regenerate', check: () => checkGeneratedState(projectRoot) },
  ];

  for (const def of checks) {
    const { check, ...meta } = def;
    records.push(await checkRecord({ ...meta, now }, check));
  }

  const overall = records.every(r => r.status === 'ok') ? 'ready' : 'incomplete';
  return { mode: 'static', overall, records };
}

// --- Re-export shared reviewer readiness ----------------------------------------

export { runReviewerReadiness } from './codex-readiness.mjs';

// --- CLI entry point --------------------------------------------------------------

function invokedAsMain() {
  return import.meta.url === `file://${resolve(process.argv[1])}`;
}

function parseArgs(argv) {
  const args = { root: process.cwd(), home: process.env.HOME, json: false, mode: 'static' };
  for (let i = 2; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === '--root' || arg === '--project-root') { args.root = argv[++i]; }
    else if (arg === '--home') { args.home = argv[++i]; }
    else if (arg === '--json') { args.json = true; }
    else if (arg === '--static') { args.mode = 'static'; }
    else if (arg === '--reviewers') { args.mode = args.mode === 'static' ? 'reviewers' : `${args.mode}+reviewers`; }
    else if (arg === '--self-test') { args.mode = 'self-test'; }
    else throw new Error(`unknown argument: ${arg}`);
  }
  return args;
}

async function main() {
  const args = parseArgs(process.argv);

  if (args.mode === 'self-test') {
    // Self-test verifies the module's own structure, not the local environment.
    // CI has no qwen binary state it can rely on. Just confirm the exports are
    // callable and return the expected shape.
    const report = await runStaticReadiness({ root: args.root, home: args.home });
    const ok = report.mode === 'static'
      && Array.isArray(report.records)
      && report.records.length > 0
      && report.records.every(r => r.id && r.class && r.status);
    if (!ok) {
      console.error('qwen-readiness self-test: FAILED');
      process.exit(1);
    }
    console.log('qwen-readiness self-test: structural checks passed');
    return;
  }

  const reports = [];
  if (args.mode.includes('static')) {
    reports.push(await runStaticReadiness({ root: args.root, home: args.home }));
  }
  if (args.mode.includes('reviewers')) {
    const { runReviewerReadiness } = await import('./codex-readiness.mjs');
    reports.push(await runReviewerReadiness({ root: args.root, home: args.home }));
  }

  const allRecords = reports.flatMap(r => r.records);
  const overall = allRecords.every(r => r.status === 'ok') ? 'ready' : 'incomplete';
  const report = {
    mode: reports.map(r => r.mode).join('+'),
    overall,
    records: allRecords,
  };

  if (args.json) {
    console.log(JSON.stringify(report));
  } else {
    for (const r of report.records) {
      console.log(`${r.status.toUpperCase()} ${r.id}`);
    }
  }
  if (overall !== 'ready') process.exit(1);
}

if (invokedAsMain()) {
  main().catch(err => {
    console.error(err);
    process.exit(1);
  });
}
