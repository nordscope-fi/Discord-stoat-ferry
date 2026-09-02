#!/usr/bin/env node

import { execFileSync, spawnSync } from 'node:child_process';
import {
  chmodSync,
  existsSync,
  lstatSync,
  mkdirSync,
  readFileSync,
  realpathSync,
  renameSync,
  rmSync,
  statSync,
  symlinkSync,
  writeFileSync,
} from 'node:fs';
import { createHash, randomUUID } from 'node:crypto';
import { dirname, join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { readProtonField } from './proton-credential.mjs';

const TOML_TRUST_PROBE = String.raw`
import json
import sys

if sys.version_info < (3, 11):
    raise SystemExit(3)

import tomllib

try:
    document = tomllib.loads(sys.stdin.read())
except tomllib.TOMLDecodeError:
    raise SystemExit(2)

projects = document.get("projects", {})
has_project = isinstance(projects, dict) and sys.argv[1] in projects
project = projects.get(sys.argv[1]) if has_project else None
trusted = isinstance(project, dict) and project.get("trust_level") == "trusted"
sys.stdout.write(json.dumps({
    "valid": True,
    "hasProject": has_project,
    "trusted": trusted,
}))
`;

const REVIEWER_AGENT = 'discord-ferry-reviewers';
const REVIEWER_VAULT = 'PortalPilot';
const CONTEXT7_AGENT = 'discord-ferry-context7';
const CONTEXT7_VAULT = 'Personal';
const CONTEXT7_ITEM = 'Context7 API Key';
const CONTEXT7_FIELD = 'API Key';
const CONTEXT7_TOKEN_FILE = 'context7-agent.pat';
const CONTEXT7_STATE_FILE = 'context7-agent.json';
const CONTEXT7_STATE_VERSION = 1;
export const REVIEWER_RUNTIME_FILES = [
  'review-contract.mjs',
  'proton-credential.mjs',
  'context7-mcp.mjs',
  'vibe-review.mjs',
  'qwen-review.mjs',
  'claude-review.mjs',
  'review-ensemble.mjs',
  'review-verification.mjs',
];
const REVIEWER_RULE_FILES = [
  'review-ensemble.mjs',
  'claude-review.mjs',
  'review-verification.mjs',
  'review-contract.mjs',
];

export function inspectProjectTrustToml(source, projectRoot) {
  const options = {
    input: source,
    encoding: 'utf8',
    stdio: ['pipe', 'pipe', 'ignore'],
    maxBuffer: 2 * 1024 * 1024,
  };
  const candidates = [
    join(process.cwd(), '.venv', 'bin', 'python3'),
    join(projectRoot, '.venv', 'bin', 'python3'),
    'python3',
  ];
  let result = null;
  const statuses = [];
  for (const candidate of [...new Set(candidates)]) {
    if (candidate.includes('/') && !existsSync(candidate)) continue;
    result = spawnSync(candidate, ['-c', TOML_TRUST_PROBE, projectRoot], options);
    statuses.push(result.status);
    if (result.status === 0 || result.status === 2) break;
  }
  if (result?.status !== 0 && result?.status !== 2) {
    result = spawnSync('uv', [
      'run', '--no-project', '--python', '>=3.11',
      'python', '-c', TOML_TRUST_PROBE, projectRoot,
    ], options);
    statuses.push(result.status);
  }
  if (result?.status !== 0) {
    throw new Error(
      'Codex config TOML is invalid; normalize the existing Ferry project trust entry before ' +
      `setup (Python statuses ${statuses.join(',') || 'unavailable'})`,
    );
  }
  const report = JSON.parse(result.stdout);
  if (report?.valid !== true || typeof report?.hasProject !== 'boolean' ||
      typeof report?.trusted !== 'boolean') {
    throw new Error('Codex config TOML probe returned an invalid result');
  }
  return report;
}

export function reconcileProjectTrust(source, projectRoot, semantic) {
  const header = `[projects.${JSON.stringify(projectRoot)}]`;
  const lines = source.split(/(?<=\n)/u);
  const starts = lines.flatMap((line, index) => {
    const match = line.match(/^\s*\[\s*projects\s*\.\s*("(?:[^"\\]|\\.)*"|'[^']*')\s*\]\s*$/u);
    if (!match) return [];
    const key = match[1].startsWith('"') ? JSON.parse(match[1]) : match[1].slice(1, -1);
    return key === projectRoot ? [index] : [];
  });
  if (starts.length > 1) throw new Error('duplicate Ferry project trust entries');
  if (semantic.hasProject && starts.length === 0) {
    throw new Error('normalize the existing Ferry project trust entry before setup');
  }
  if (starts.length === 0) {
    const gap = source.length === 0 || source.endsWith('\n\n')
      ? ''
      : source.endsWith('\n') ? '\n' : '\n\n';
    return `${source}${gap}${header}\ntrust_level = "trusted"\n`;
  }
  const start = starts[0];
  let end = lines.findIndex((line, index) => index > start && /^\s*\[/u.test(line));
  if (end === -1) end = lines.length;
  const trust = lines.slice(start + 1, end)
    .flatMap((line, index) => /^\s*trust_level\s*=/u.test(line) ? [start + 1 + index] : []);
  if (trust.length > 1) throw new Error('duplicate trust_level keys in Ferry project entry');
  if (trust.length === 1) lines[trust[0]] = 'trust_level = "trusted"\n';
  else lines.splice(start + 1, 0, 'trust_level = "trusted"\n');
  return lines.join('');
}

export function canonicalCheckoutRoot(root) {
  const commonDir = execFileSync(
    'git',
    ['rev-parse', '--path-format=absolute', '--git-common-dir'],
    { cwd: root, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] },
  ).trim();
  return dirname(realpathSync(commonDir));
}

function writeAtomic(path, content, beforeRename, forcedMode = null) {
  const parent = dirname(path);
  mkdirSync(parent, { recursive: true });
  const mode = forcedMode ?? (existsSync(path) ? statSync(path).mode & 0o777 : 0o600);
  const temporary = join(parent, `.${path.split('/').at(-1)}.${process.pid}.${randomUUID()}.tmp`);
  try {
    writeFileSync(temporary, content, { encoding: 'utf8', mode: 0o600, flag: 'wx' });
    chmodSync(temporary, mode);
    const staged = readFileSync(temporary, 'utf8');
    if (staged !== content) throw new Error('staged Codex config validation failed');
    beforeRename?.();
    renameSync(temporary, path);
  } catch (error) {
    rmSync(temporary, { force: true });
    throw error;
  }
}

function lstatOrNull(path) {
  try {
    return lstatSync(path);
  } catch (error) {
    if (error?.code === 'ENOENT') return null;
    throw error;
  }
}

export function reviewerRuntimeRoot(home, xdgDataHome = process.env.XDG_DATA_HOME) {
  const dataRoot = xdgDataHome || join(home, '.local', 'share');
  return join(dataRoot, 'discord-ferry', 'reviewer-runtime');
}

export function reviewerRuntimeSource(root) {
  return Object.fromEntries(REVIEWER_RUNTIME_FILES.map((name) => [
    name,
    readFileSync(join(root, 'scripts', 'agent-compat', name)),
  ]));
}

function runtimeManifest(files) {
  const hashes = {};
  const releaseHash = createHash('sha256');
  for (const name of Object.keys(files).sort()) {
    const bytes = Buffer.isBuffer(files[name]) ? files[name] : Buffer.from(files[name]);
    hashes[name] = createHash('sha256').update(bytes).digest('hex');
    releaseHash.update(name).update('\0').update(bytes).update('\0');
  }
  return { version: 1, release: releaseHash.digest('hex'), files: hashes };
}

function validateRuntimeRelease(releasePath, expected) {
  const releaseStat = lstatSync(releasePath);
  if (!releaseStat.isDirectory() || releaseStat.isSymbolicLink()) {
    throw new Error('reviewer runtime release is not a directory');
  }
  const manifestPath = join(releasePath, 'manifest.json');
  if ((statSync(manifestPath).mode & 0o222) !== 0) {
    throw new Error('reviewer runtime manifest is writable');
  }
  const actual = JSON.parse(readFileSync(manifestPath, 'utf8'));
  if (JSON.stringify(actual) !== JSON.stringify(expected)) {
    throw new Error('reviewer runtime manifest mismatch');
  }
  for (const [name, expectedHash] of Object.entries(expected.files)) {
    const path = join(releasePath, name);
    const stat = statSync(path);
    if (!stat.isFile()) throw new Error('reviewer runtime entry is not a file');
    if ((stat.mode & 0o222) !== 0) throw new Error('reviewer runtime file is writable');
    const hash = createHash('sha256').update(readFileSync(path)).digest('hex');
    if (hash !== expectedHash) throw new Error('reviewer runtime file hash mismatch');
  }
}

export function renderReviewerRules(currentPath) {
  return `${REVIEWER_RULE_FILES.map((name) =>
    `prefix_rule(pattern=${JSON.stringify(['node', join(currentPath, name)])}, decision="allow")`
  ).join('\n')}\n`;
}

export function verifyReviewerRuntime({
  home,
  root,
  files = reviewerRuntimeSource(root),
  xdgDataHome = process.env.XDG_DATA_HOME,
  part = 'all',
}) {
  const runtimeRoot = reviewerRuntimeRoot(home, xdgDataHome);
  const currentPath = join(runtimeRoot, 'current');
  const currentStat = lstatOrNull(currentPath);
  if (!currentStat?.isSymbolicLink()) throw new Error('reviewer runtime current link is missing');
  const releasePath = realpathSync(currentPath);
  const releasesPath = realpathSync(join(runtimeRoot, 'releases'));
  if (dirname(releasePath) !== releasesPath) {
    throw new Error('reviewer runtime current link leaves releases');
  }
  const manifest = runtimeManifest(files);
  if (releasePath !== join(releasesPath, manifest.release)) {
    throw new Error('reviewer runtime release is stale');
  }
  if (part !== 'rules') validateRuntimeRelease(releasePath, manifest);
  const rulesPath = join(home, '.codex', 'rules', 'ferry-reviewers.rules');
  if (part !== 'runtime') {
    const rules = readFileSync(rulesPath, 'utf8');
    if (rules !== renderReviewerRules(currentPath)) {
      throw new Error('reviewer command rules mismatch');
    }
    if ((statSync(rulesPath).mode & 0o777) !== 0o600) {
      throw new Error('reviewer command rules mode mismatch');
    }
  }
  return { release: manifest.release, files: Object.keys(manifest.files).length };
}

export function installReviewerRuntime({
  home,
  root = null,
  files = root ? reviewerRuntimeSource(root) : null,
  dryRun = false,
  beforeActivate = null,
  xdgDataHome = process.env.XDG_DATA_HOME,
}) {
  if (!home || !files) throw new Error('reviewer runtime install requires home and files');
  if (Object.keys(files).sort().join('\n') !== [...REVIEWER_RUNTIME_FILES].sort().join('\n')) {
    throw new Error('reviewer runtime source list mismatch');
  }
  const runtimeRoot = reviewerRuntimeRoot(home, xdgDataHome);
  const releasesPath = join(runtimeRoot, 'releases');
  const currentPath = join(runtimeRoot, 'current');
  const rulesPath = join(home, '.codex', 'rules', 'ferry-reviewers.rules');
  const manifest = runtimeManifest(files);
  const releasePath = join(releasesPath, manifest.release);
  const rules = renderReviewerRules(currentPath);
  const currentStat = lstatOrNull(currentPath);
  if (currentStat && !currentStat.isSymbolicLink()) {
    throw new Error('reviewer runtime current entry is not a symlink');
  }
  const active = currentStat ? realpathSync(currentPath) : null;
  const existingRules = existsSync(rulesPath) ? readFileSync(rulesPath, 'utf8') : null;
  if (dryRun) {
    return {
      release: manifest.release,
      currentPath,
      rulesPath,
      changed: active !== releasePath || existingRules !== rules,
      dryRun: true,
    };
  }

  mkdirSync(releasesPath, { recursive: true, mode: 0o700 });
  const releasesStat = lstatSync(releasesPath);
  if (!releasesStat.isDirectory() || releasesStat.isSymbolicLink()) {
    throw new Error('reviewer runtime releases entry is not a directory');
  }
  if (!existsSync(releasePath)) {
    const staged = join(releasesPath, `.${manifest.release}.${process.pid}.${randomUUID()}.tmp`);
    try {
      mkdirSync(staged, { mode: 0o700 });
      for (const name of REVIEWER_RUNTIME_FILES) {
        const bytes = Buffer.isBuffer(files[name]) ? files[name] : Buffer.from(files[name]);
        writeFileSync(join(staged, name), bytes, { mode: 0o400, flag: 'wx' });
      }
      writeFileSync(join(staged, 'manifest.json'), `${JSON.stringify(manifest)}\n`, {
        mode: 0o400,
        flag: 'wx',
      });
      validateRuntimeRelease(staged, manifest);
      chmodSync(staged, 0o500);
      renameSync(staged, releasePath);
    } catch (error) {
      rmSync(staged, { recursive: true, force: true });
      throw error;
    }
  } else validateRuntimeRelease(releasePath, manifest);

  if (active !== releasePath) {
    const stagedLink = join(runtimeRoot, `.current.${process.pid}.${randomUUID()}.tmp`);
    try {
      symlinkSync(join('releases', manifest.release), stagedLink);
      beforeActivate?.();
      renameSync(stagedLink, currentPath);
    } catch (error) {
      rmSync(stagedLink, { force: true });
      throw error;
    }
  }
  validateRuntimeRelease(realpathSync(currentPath), manifest);
  if (existingRules !== rules) writeAtomic(rulesPath, rules, null, 0o600);
  chmodSync(rulesPath, 0o600);
  verifyReviewerRuntime({ home, root, files, xdgDataHome });
  return {
    release: manifest.release,
    currentPath,
    rulesPath,
    changed: active !== releasePath || existingRules !== rules,
    dryRun: false,
  };
}

export function parseAgentToken(stdout) {
  let envelope;
  try {
    envelope = JSON.parse(stdout);
  } catch {
    throw new Error('Proton agent command returned an unrecognized token envelope');
  }
  const prefix = 'PROTON_PASS_PERSONAL_ACCESS_TOKEN=';
  const matches = [];
  const visit = (value) => {
    if (typeof value === 'string') {
      if (value.startsWith(prefix)) matches.push(value.slice(prefix.length));
      else if (/^pst_[^\s]{32,}$/u.test(value)) matches.push(value);
      return;
    }
    if (Array.isArray(value)) {
      for (const child of value) visit(child);
      return;
    }
    if (value && typeof value === 'object') {
      for (const child of Object.values(value)) visit(child);
    }
  };
  visit(envelope);
  const tokens = [...new Set(matches)];
  if (tokens.length !== 1) {
    throw new Error('Proton agent command returned an unrecognized token envelope');
  }
  const value = tokens[0];
  if (!/^pst_[^\s]{32,}$/u.test(value)) {
    throw new Error('Proton agent command returned an invalid token value');
  }
  return value;
}

function runPassCli(passCli, args) {
  const result = spawnSync(passCli, args, {
    encoding: 'utf8',
    stdio: ['ignore', 'pipe', 'pipe'],
    env: {
      ...process.env,
      PROTON_PASS_AGENT_REASON: 'Configure Discord Ferry reviewer access',
    },
    maxBuffer: 2 * 1024 * 1024,
  });
  if (result.status !== 0) {
    throw new Error(`pass-cli command failed: ${args.slice(0, 2).join(' ')}`);
  }
  return result.stdout;
}

function parseValueFreeList(stdout, label, envelopeKey) {
  try {
    const value = JSON.parse(stdout);
    if (Array.isArray(value)) return value;
    if (value && Array.isArray(value[envelopeKey])) return value[envelopeKey];
    throw new Error('not a supported list');
  } catch {
    throw new Error(`pass-cli ${label} returned an invalid JSON list`);
  }
}

export async function provisionReviewerAgent({ home, passCli }) {
  if (!home || !passCli) throw new Error('reviewer agent setup requires home and pass-cli');
  const help = runPassCli(passCli, ['agent', 'create', '--help']);
  if (!/\bNAME\b/u.test(help) || !help.includes('--expiration') ||
      !/\b3m\b/u.test(help) || !help.includes('--vault')) {
    throw new Error('installed pass-cli does not support the required scoped agent command');
  }

  const vaults = parseValueFreeList(
    runPassCli(passCli, ['vault', 'list', '--output', 'json']),
    'vault list',
    'vaults',
  );
  if (vaults.filter((vault) => vault?.name === REVIEWER_VAULT).length !== 1) {
    throw new Error('pass-cli must expose exactly one PortalPilot vault');
  }

  const agents = parseValueFreeList(
    runPassCli(passCli, ['agent', 'list', '--output', 'json']),
    'agent list',
    'agents',
  );
  const matchingAgents = agents.filter((agent) => agent?.name === REVIEWER_AGENT);
  if (matchingAgents.length > 1) {
    throw new Error('multiple discord-ferry-reviewers agents exist; remove duplicates before setup');
  }
  const agentExists = matchingAgents.length === 1;
  const tokenPath = join(home, '.config', 'discord-ferry', 'reviewer-agent.pat');
  const tokenExists = existsSync(tokenPath);
  const expireTime = Number(matchingAgents[0]?.expire_time);
  const agentExpired = agentExists && Number.isFinite(expireTime)
    && expireTime <= Math.floor(Date.now() / 1000);

  if (agentExists && tokenExists && !agentExpired) return { created: false, renewed: false };
  if (agentExists !== tokenExists) {
    throw new Error(
      'reviewer agent state is incomplete; run: pass-cli agent renew --expiration 3m ' +
      '--output json discord-ferry-reviewers',
    );
  }

  if (agentExpired) {
    const renewed = runPassCli(passCli, [
      'agent', 'renew', '--expiration', '3m', '--output', 'json', REVIEWER_AGENT,
    ]);
    const token = parseAgentToken(renewed);
    writeAtomic(tokenPath, token, null);
    return { created: false, renewed: true };
  }

  const created = runPassCli(passCli, [
    'agent', 'create', REVIEWER_AGENT, '--expiration', '3m', '--vault', REVIEWER_VAULT,
  ]);
  const token = parseAgentToken(created);
  writeAtomic(tokenPath, token, null);
  return { created: true, renewed: false };
}

function context7GrantDigest() {
  return createHash('sha256').update(JSON.stringify({
    vault: CONTEXT7_VAULT,
    item: CONTEXT7_ITEM,
    role: 'viewer',
  })).digest('hex');
}

function context7Ownership(agent, state) {
  return {
    version: CONTEXT7_STATE_VERSION,
    agent_id: String(agent.id),
    agent_name: CONTEXT7_AGENT,
    state,
    grant_sha256: context7GrantDigest(),
  };
}

function secureRegularFile(path, label) {
  const info = lstatOrNull(path);
  if (!info) return null;
  if (!info.isFile() || info.isSymbolicLink()) throw new Error(`${label} must be a regular file`);
  if ((info.mode & 0o777) !== 0o600) throw new Error(`${label} must have mode 0600`);
  return info;
}

function readContext7Ownership(path) {
  if (!secureRegularFile(path, CONTEXT7_STATE_FILE)) return null;
  let document;
  try {
    document = JSON.parse(readFileSync(path, 'utf8'));
  } catch {
    throw new Error('Context7 ownership record is invalid');
  }
  const keys = Object.keys(document ?? {}).sort();
  const expectedKeys = ['agent_id', 'agent_name', 'grant_sha256', 'state', 'version'];
  if (JSON.stringify(keys) !== JSON.stringify(expectedKeys)
      || document.version !== CONTEXT7_STATE_VERSION
      || typeof document.agent_id !== 'string'
      || document.agent_id.length === 0
      || document.agent_name !== CONTEXT7_AGENT
      || !['creating', 'ready'].includes(document.state)
      || document.grant_sha256 !== context7GrantDigest()) {
    throw new Error('Context7 ownership record is invalid');
  }
  return document;
}

function parseCreatedContext7Agent(stdout) {
  let envelope;
  try {
    envelope = JSON.parse(stdout);
  } catch {
    throw new Error('Proton agent command returned an invalid Context7 identity');
  }
  const agent = envelope?.agent;
  if (!agent || agent.name !== CONTEXT7_AGENT
      || !['string', 'number'].includes(typeof agent.id)
      || String(agent.id).length === 0) {
    throw new Error('Proton agent command returned an invalid Context7 identity');
  }
  return { agent, token: parseAgentToken(stdout) };
}

function writeContext7Ownership(path, agent, state) {
  writeAtomic(path, `${JSON.stringify(context7Ownership(agent, state))}\n`, null, 0o600);
}

async function verifyContext7Field({ home, fieldReader }) {
  await fieldReader({
    tokenFile: CONTEXT7_TOKEN_FILE,
    vaultName: CONTEXT7_VAULT,
    itemTitle: CONTEXT7_ITEM,
    field: CONTEXT7_FIELD,
    reason: 'Verify Discord Ferry Context7 access',
    home,
  });
}

async function createContext7Agent({
  home,
  passCli,
  run,
  fieldReader,
  tokenPath,
  ownershipPath,
  recovered,
}) {
  const created = parseCreatedContext7Agent(run(passCli, [
    'agent', 'create', CONTEXT7_AGENT, '--expiration', '3m',
  ]));
  writeContext7Ownership(ownershipPath, created.agent, 'creating');
  writeAtomic(tokenPath, created.token, null, 0o600);
  run(passCli, [
    'agent', 'access', 'grant', CONTEXT7_AGENT,
    '--vault-name', CONTEXT7_VAULT,
    '--item-title', CONTEXT7_ITEM,
    '--role', 'viewer',
  ]);
  await verifyContext7Field({ home, fieldReader });
  writeContext7Ownership(ownershipPath, created.agent, 'ready');
  return { created: true, renewed: false, recovered };
}

export async function provisionContext7Agent({
  home,
  passCli,
  run = runPassCli,
  fieldReader = readProtonField,
  now = () => Date.now(),
}) {
  if (!home || !passCli) throw new Error('Context7 agent setup requires home and pass-cli');
  const help = run(passCli, ['agent', 'access', 'grant', '--help']);
  if (!help.includes('--item-title') || !help.includes('--role')) {
    throw new Error('installed pass-cli does not support item-limited agent access');
  }
  const vaults = parseValueFreeList(
    run(passCli, ['vault', 'list', '--output', 'json']),
    'vault list',
    'vaults',
  );
  if (vaults.filter((vault) => vault?.name === CONTEXT7_VAULT).length !== 1) {
    throw new Error('pass-cli must expose exactly one Personal vault');
  }
  const items = parseValueFreeList(
    run(passCli, ['item', 'list', '--vault-name', CONTEXT7_VAULT, '--output', 'json']),
    'item list',
    'items',
  );
  if (items.filter((item) => item?.title === CONTEXT7_ITEM).length !== 1) {
    throw new Error('pass-cli must expose exactly one Context7 API Key item');
  }
  const agents = parseValueFreeList(
    run(passCli, ['agent', 'list', '--output', 'json']),
    'agent list',
    'agents',
  );
  const matchingAgents = agents.filter((agent) => agent?.name === CONTEXT7_AGENT);
  if (matchingAgents.length > 1) {
    throw new Error('multiple discord-ferry-context7 agents exist; remove duplicates before setup');
  }

  const credentialDirectory = join(home, '.config', 'discord-ferry');
  const tokenPath = join(credentialDirectory, CONTEXT7_TOKEN_FILE);
  const ownershipPath = join(credentialDirectory, CONTEXT7_STATE_FILE);
  const ownership = readContext7Ownership(ownershipPath);
  if (matchingAgents.length === 1) {
    const agent = matchingAgents[0];
    if (!ownership || ownership.agent_id !== String(agent.id)) {
      throw new Error('Context7 agent exists without a matching ownership record');
    }
    if (ownership.state === 'creating') {
      run(passCli, ['agent', 'delete', CONTEXT7_AGENT]);
      rmSync(tokenPath, { force: true });
      rmSync(ownershipPath, { force: true });
      return createContext7Agent({
        home,
        passCli,
        run,
        fieldReader,
        tokenPath,
        ownershipPath,
        recovered: true,
      });
    }
    if (!secureRegularFile(tokenPath, CONTEXT7_TOKEN_FILE)) {
      throw new Error('Context7 token file is missing');
    }
    const expireTime = Number(agent.expire_time);
    const expired = Number.isFinite(expireTime)
      && expireTime <= Math.floor(now() / 1000);
    if (expired) {
      const renewed = run(passCli, [
        'agent', 'renew', '--expiration', '3m', '--output', 'json', CONTEXT7_AGENT,
      ]);
      writeAtomic(tokenPath, parseAgentToken(renewed), null, 0o600);
      await verifyContext7Field({ home, fieldReader });
      return { created: false, renewed: true, recovered: false };
    }
    await verifyContext7Field({ home, fieldReader });
    return { created: false, renewed: false, recovered: false };
  }
  if (ownership || lstatOrNull(tokenPath)) {
    throw new Error('Context7 local credential state has no matching agent');
  }

  return createContext7Agent({
    home,
    passCli,
    run,
    fieldReader,
    tokenPath,
    ownershipPath,
    recovered: false,
  });
}

export async function provisionAgentCredentials({
  home,
  passCli,
  reviewer = provisionReviewerAgent,
  context7 = provisionContext7Agent,
}) {
  const reviewerReport = await reviewer({ home, passCli });
  const context7Report = await context7({ home, passCli });
  return { reviewer: reviewerReport, context7: context7Report };
}

function quoteShellArgument(argument) {
  if (/^[A-Za-z0-9_@%+=:,./-]+$/.test(argument)) return argument;
  return `'${argument.replaceAll("'", `'"'"'`)}'`;
}

function renderShellCommand(arguments_) {
  return arguments_.map(quoteShellArgument).join(' ');
}

export function claudeContext7OwnerAction(home, currentPath = null) {
  const runtimePath = currentPath ?? join(reviewerRuntimeRoot(home), 'current');
  const launcher = join(runtimePath, 'context7-mcp.mjs');
  return {
    launcher,
    commands: [
      ['claude', 'mcp', 'remove', '--scope', 'project', 'context7'],
      ['claude', 'mcp', 'add', '--scope', 'project', 'context7', '--', 'node', launcher],
    ],
  };
}

export function renderBootstrapMessage(report) {
  const commands = report.claudeContext7.commands.map(renderShellCommand);
  return [
    `Codex trust ready for ${report.canonicalRoot}`,
    'Context7 credential ready.',
    'Run these commands from the project root:',
    ...commands,
    'Fresh Claude Code sessions use the protected Context7 launcher after you apply them.',
  ].join('\n');
}

function resolveInstalledPassCli(home) {
  const installed = join(home, '.local', 'bin', 'pass-cli');
  if (!existsSync(installed)) {
    throw new Error('pass-cli is not installed at ~/.local/bin/pass-cli');
  }
  return realpathSync(installed);
}

export async function runBootstrap({
  home,
  root,
  dryRun = false,
  proton = async () => {},
  beforeRename = null,
  canonicalRoot = null,
  runtime = installReviewerRuntime,
}) {
  if (!home || !root) throw new Error('bootstrap requires home and root');
  const ownedRoot = resolve(canonicalRoot ?? canonicalCheckoutRoot(root));
  const configPath = join(home, '.codex', 'config.toml');
  const source = existsSync(configPath) ? readFileSync(configPath, 'utf8') : '';
  const semantic = inspectProjectTrustToml(source, ownedRoot);
  const reconciled = reconcileProjectTrust(source, ownedRoot, semantic);

  if (!dryRun && reconciled !== source) {
    inspectProjectTrustToml(reconciled, ownedRoot);
    writeAtomic(configPath, reconciled, beforeRename);
    inspectProjectTrustToml(readFileSync(configPath, 'utf8'), ownedRoot);
  }
  const runtimeReport = await runtime({ home, root: resolve(root), dryRun });
  const credentialReport = dryRun
    ? null
    : await proton({ home, root, canonicalRoot: ownedRoot });
  return {
    changed: reconciled !== source,
    canonicalRoot: ownedRoot,
    runtime: runtimeReport,
    credentials: credentialReport,
    claudeContext7: claudeContext7OwnerAction(home, runtimeReport.currentPath),
  };
}

function selfTest() {
  const root = '/tmp/ferry-bootstrap-self-test';
  const source = 'model = "personal"\n';
  const first = reconcileProjectTrust(source, root, { valid: true, hasProject: false });
  const second = reconcileProjectTrust(first, root, { valid: true, hasProject: true });
  if (first !== second || !first.includes(`[projects.${JSON.stringify(root)}]`)) {
    throw new Error('project trust reconciliation is not stable');
  }
  process.stdout.write('codex-bootstrap self-test: all checks passed\n');
}

function option(args, name) {
  const index = args.indexOf(name);
  return index === -1 ? null : args[index + 1];
}

async function main() {
  const args = process.argv.slice(2);
  if (args.includes('--self-test')) {
    selfTest();
    return;
  }
  const known = new Set(['--home', '--root', '--dry-run', '--json']);
  for (let index = 0; index < args.length; index += 1) {
    const argument = args[index];
    if (!known.has(argument)) throw new Error(`unknown argument: ${argument}`);
    if (argument === '--home' || argument === '--root') index += 1;
  }
  const home = option(args, '--home') ?? process.env.HOME;
  const root = option(args, '--root') ?? process.cwd();
  const report = await runBootstrap({
    home,
    root,
    dryRun: args.includes('--dry-run'),
    proton: ({ home: bootstrapHome }) => provisionAgentCredentials({
      home: bootstrapHome,
      passCli: resolveInstalledPassCli(bootstrapHome),
    }),
  });
  if (args.includes('--json')) process.stdout.write(`${JSON.stringify(report)}\n`);
  else process.stdout.write(`${renderBootstrapMessage(report)}\n`);
}

let invokedAsMain = false;
if (process.argv[1]) {
  try {
    invokedAsMain = import.meta.url === pathToFileURL(realpathSync(process.argv[1])).href;
  } catch { /* an invalid entrypoint cannot be the current module */ }
}

if (invokedAsMain) {
  main().catch((error) => {
    process.stderr.write(`codex-bootstrap: ${error.message}\n`);
    process.exitCode = 1;
  });
}
