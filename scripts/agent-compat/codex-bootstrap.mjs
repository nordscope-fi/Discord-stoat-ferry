#!/usr/bin/env node

import { execFileSync, spawnSync } from 'node:child_process';
import {
  chmodSync,
  closeSync,
  existsSync,
  lstatSync,
  mkdtempSync,
  mkdirSync,
  openSync,
  readFileSync,
  realpathSync,
  renameSync,
  rmSync,
  statSync,
  symlinkSync,
  writeFileSync,
} from 'node:fs';
import { createHash, randomUUID } from 'node:crypto';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import {
  REVIEWER_PROVIDERS,
  REVIEWER_STATE_FILE,
  REVIEWER_STATE_VERSION,
  readProtonField,
  readReviewerOwnership,
  reviewerGrantDigest,
} from './proton-credential.mjs';

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
const REVIEWER_VAULT = 'Personal';
const REVIEWER_LEGACY_VAULT = 'PortalPilot';
const REVIEWER_TOKEN_FILE = 'reviewer-agent.pat';
const REVIEWER_STAGING_FILE = 'reviewer-agent.create.json';
const REVIEWER_FIELD = 'API Key';
const REVIEWER_ITEMS = Object.freeze({
  vibe: 'Mistral Vibe API Key',
  qwen: 'QwenCloud API Key',
});
const CONTEXT7_AGENT = 'discord-ferry-context7';
const CONTEXT7_VAULT = 'Personal';
const CONTEXT7_ITEM = 'Context7 API Key';
const CONTEXT7_FIELD = 'API Key';
const CONTEXT7_TOKEN_FILE = 'context7-agent.pat';
const CONTEXT7_STATE_FILE = 'context7-agent.json';
const CONTEXT7_STATE_VERSION = 2;
const CONTEXT7_PENDING_AGENT_ID = 'pending';
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

function runPassCliToFile(passCli, args, outputPath) {
  mkdirSync(dirname(outputPath), { recursive: true });
  const output = openSync(outputPath, 'wx', 0o600);
  let result;
  try {
    result = spawnSync(passCli, args, {
      encoding: 'utf8',
      stdio: ['ignore', output, 'pipe'],
      env: {
        ...process.env,
        PROTON_PASS_AGENT_REASON: 'Configure Discord Ferry reviewer access',
      },
      maxBuffer: 2 * 1024 * 1024,
    });
  } finally {
    closeSync(output);
  }
  if (result.status !== 0) {
    rmSync(outputPath, { force: true });
    throw new Error(`pass-cli command failed: ${args.slice(0, 2).join(' ')}`);
  }
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

function reviewerAgentId(agent) {
  const id = agent?.pat_id ?? agent?.id;
  if (!['string', 'number'].includes(typeof id) || String(id).length === 0) {
    throw new Error('Proton agent list returned an invalid reviewer identity');
  }
  return String(id);
}

function emptyReviewerItems() {
  return Object.fromEntries(REVIEWER_PROVIDERS.map((provider) => [
    provider, { share_id: null, item_id: null },
  ]));
}

function reviewerOwnership(agentId, state, items, legacyShareId = null) {
  return {
    version: REVIEWER_STATE_VERSION,
    agent_id: agentId,
    agent_name: REVIEWER_AGENT,
    state,
    grant_sha256: reviewerGrantDigest(),
    legacy_share_id: legacyShareId,
    items,
  };
}

function writeReviewerOwnership(path, agentId, state, items, legacyShareId = null) {
  const document = reviewerOwnership(agentId, state, items, legacyShareId);
  writeAtomic(path, `${JSON.stringify(document)}\n`, null, 0o600);
  return document;
}

function readReviewerCreation(path) {
  secureRegularFile(path, REVIEWER_STAGING_FILE);
  let document;
  try {
    document = JSON.parse(readFileSync(path, 'utf8'));
  } catch {
    throw new Error('Reviewer creation response is invalid');
  }
  if (JSON.stringify(Object.keys(document ?? {}).sort()) !==
      JSON.stringify(['instruction', 'token'])
      || typeof document.instruction !== 'string'
      || document.instruction.length === 0) {
    throw new Error('Reviewer creation response is invalid');
  }
  return parseAgentToken(JSON.stringify(document));
}

async function authenticateReviewerToken(passCli, token) {
  const sessionDirectory = mkdtempSync(join(tmpdir(), 'ferry-reviewer-auth-'));
  try {
    const result = spawnSync(passCli, ['login'], {
      encoding: 'utf8',
      stdio: ['ignore', 'ignore', 'ignore'],
      env: {
        PATH: process.env.PATH ?? '',
        PROTON_PASS_SESSION_DIR: sessionDirectory,
        PROTON_PASS_PERSONAL_ACCESS_TOKEN: token,
      },
      maxBuffer: 2 * 1024 * 1024,
    });
    if (result.status !== 0) throw new Error('Reviewer creation token could not authenticate');
  } finally {
    rmSync(sessionDirectory, { recursive: true, force: true });
  }
}

function listReviewerAccess(passCli, run) {
  return parseValueFreeList(run(passCli, [
    'personal-access-token', 'access', 'list-access',
    '--personal-access-token-name', REVIEWER_AGENT,
    '--output', 'json',
  ]), 'access list', 'accesses');
}

function inspectReviewerAccess(
  grants,
  expectedItemIds,
  legacyShareId = null,
  allowMissingLegacy = false,
) {
  const items = emptyReviewerItems();
  let legacySeen = false;
  for (const grant of grants) {
    const provider = REVIEWER_PROVIDERS.find(
      (candidate) => REVIEWER_ITEMS[candidate] === grant?.item_title,
    );
    if (provider && grant?.type === 'item' && grant?.role === 'Viewer'
        && grant?.item_id === expectedItemIds[provider]
        && typeof grant.share_id === 'string' && grant.share_id.length > 0
        && typeof grant.item_id === 'string' && grant.item_id.length > 0) {
      if (items[provider].share_id !== null) {
        throw new Error('Reviewer access grants are ambiguous');
      }
      items[provider] = { share_id: grant.share_id, item_id: grant.item_id };
      continue;
    }
    if (legacyShareId !== null
        && grant?.type === 'vault'
        && grant?.role === 'Viewer'
        && grant?.vault_name === REVIEWER_LEGACY_VAULT
        && grant?.share_id === legacyShareId
        && !legacySeen) {
      legacySeen = true;
      continue;
    }
    throw new Error('Reviewer access grants are invalid');
  }
  if (legacyShareId !== null && !legacySeen && !allowMissingLegacy) {
    throw new Error('Reviewer legacy access grant is missing');
  }
  return items;
}

function reviewerLocatorsComplete(items) {
  return REVIEWER_PROVIDERS.every((provider) => items[provider].share_id !== null);
}

function reviewerLegacyShare(grants) {
  if (grants.length !== 1) return null;
  const grant = grants[0];
  if (grant?.type !== 'vault'
      || grant?.role !== 'Viewer'
      || grant?.vault_name !== REVIEWER_LEGACY_VAULT
      || typeof grant?.share_id !== 'string'
      || grant.share_id.length === 0) return null;
  return grant.share_id;
}

function assertSavedReviewerLocators(ownership, items) {
  for (const provider of REVIEWER_PROVIDERS) {
    const saved = ownership.items[provider];
    const actual = items[provider];
    if (saved.share_id !== null
        && (saved.share_id !== actual.share_id || saved.item_id !== actual.item_id)) {
      throw new Error('Reviewer access identifiers changed');
    }
  }
}

async function verifyReviewerFields({ home, fieldReader, items }) {
  for (const provider of REVIEWER_PROVIDERS) {
    const locator = items[provider];
    await fieldReader({
      tokenFile: REVIEWER_TOKEN_FILE,
      shareId: locator.share_id,
      itemId: locator.item_id,
      field: REVIEWER_FIELD,
      reason: `Verify Discord Ferry ${provider} reviewer access`,
      home,
    });
  }
}

async function reconcileReviewerGrants({
  home,
  passCli,
  run,
  fieldReader,
  ownershipPath,
  ownership,
  expectedItemIds,
}) {
  let grants = listReviewerAccess(passCli, run);
  const legacyMayAlreadyBeRevoked = ownership.legacy_share_id !== null
    && reviewerLocatorsComplete(ownership.items);
  let legacyPresent = ownership.legacy_share_id !== null
    && grants.some((grant) => grant?.share_id === ownership.legacy_share_id);
  let items = inspectReviewerAccess(
    grants, expectedItemIds, ownership.legacy_share_id, legacyMayAlreadyBeRevoked,
  );
  assertSavedReviewerLocators(ownership, items);
  for (const provider of REVIEWER_PROVIDERS) {
    if (items[provider].share_id !== null) continue;
    run(passCli, [
      'agent', 'access', 'grant', REVIEWER_AGENT,
      '--vault-name', REVIEWER_VAULT,
      '--item-title', REVIEWER_ITEMS[provider],
      '--role', 'viewer',
    ]);
  }
  grants = listReviewerAccess(passCli, run);
  legacyPresent = ownership.legacy_share_id !== null
    && grants.some((grant) => grant?.share_id === ownership.legacy_share_id);
  items = inspectReviewerAccess(
    grants, expectedItemIds, ownership.legacy_share_id, legacyMayAlreadyBeRevoked,
  );
  if (!reviewerLocatorsComplete(items)) {
    throw new Error('Reviewer item access grant is missing');
  }
  assertSavedReviewerLocators(ownership, items);
  const provisioning = reviewerOwnership(
    ownership.agent_id, 'provisioning', items, ownership.legacy_share_id,
  );
  if (JSON.stringify(ownership) !== JSON.stringify(provisioning)) {
    writeReviewerOwnership(
      ownershipPath, ownership.agent_id, 'provisioning', items, ownership.legacy_share_id,
    );
  }
  await verifyReviewerFields({ home, fieldReader, items });
  if (legacyPresent) {
    run(passCli, [
      'agent', 'access', 'revoke', '--share-id', ownership.legacy_share_id, REVIEWER_AGENT,
    ]);
    const afterRevoke = listReviewerAccess(passCli, run);
    const finalItems = inspectReviewerAccess(afterRevoke, expectedItemIds);
    if (!reviewerLocatorsComplete(finalItems)) {
      throw new Error('Reviewer item access grant is missing after legacy revocation');
    }
    items = finalItems;
  }
  writeReviewerOwnership(ownershipPath, ownership.agent_id, 'ready', items);
  return items;
}

export async function provisionReviewerAgent({
  home,
  passCli,
  run = runPassCli,
  createRunner = runPassCliToFile,
  fieldReader = readProtonField,
  tokenAuthenticator = authenticateReviewerToken,
  now = () => Date.now(),
}) {
  if (!home || !passCli) throw new Error('reviewer agent setup requires home and pass-cli');
  const createHelp = run(passCli, ['agent', 'create', '--help']);
  const grantHelp = run(passCli, ['agent', 'access', 'grant', '--help']);
  if (!/\bNAME\b/u.test(createHelp) || !createHelp.includes('--expiration')
      || !grantHelp.includes('--item-title') || !grantHelp.includes('--role')) {
    throw new Error('installed pass-cli does not support exact reviewer item access');
  }
  const vaults = parseValueFreeList(
    run(passCli, ['vault', 'list', '--output', 'json']), 'vault list', 'vaults',
  );
  if (vaults.filter((vault) => vault?.name === REVIEWER_VAULT).length !== 1) {
    throw new Error('pass-cli must expose exactly one Personal vault');
  }
  const availableItems = parseValueFreeList(
    run(passCli, ['item', 'list', '--vault-name', REVIEWER_VAULT, '--output', 'json']),
    'item list',
    'items',
  );
  const expectedItemIds = {};
  for (const provider of REVIEWER_PROVIDERS) {
    const title = REVIEWER_ITEMS[provider];
    const matches = availableItems.filter((item) => item?.title === title);
    if (matches.length !== 1 || typeof matches[0]?.id !== 'string' || !matches[0].id) {
      throw new Error(`pass-cli must expose exactly one identifiable ${title} item`);
    }
    expectedItemIds[provider] = matches[0].id;
  }

  const directory = join(home, '.config', 'discord-ferry');
  const tokenPath = join(directory, REVIEWER_TOKEN_FILE);
  const ownershipPath = join(directory, REVIEWER_STATE_FILE);
  const stagingPath = join(directory, REVIEWER_STAGING_FILE);
  mkdirSync(directory, { recursive: true, mode: 0o700 });
  let agents = parseValueFreeList(
    run(passCli, ['agent', 'list', '--output', 'json']), 'agent list', 'agents',
  ).filter((agent) => agent?.name === REVIEWER_AGENT);
  if (agents.length > 1) {
    throw new Error('multiple discord-ferry-reviewers agents exist; remove duplicates before setup');
  }
  const ownershipEntry = lstatOrNull(ownershipPath);
  let ownership = ownershipEntry ? readReviewerOwnership(home) : null;
  const tokenEntry = secureRegularFile(tokenPath, REVIEWER_TOKEN_FILE);
  const stagingEntry = lstatOrNull(stagingPath);
  let created = false;
  let recovered = false;
  let migrated = false;

  if (stagingEntry) {
    const stagedToken = readReviewerCreation(stagingPath);
    if (ownership || agents.length !== 1) {
      throw new Error('Reviewer staged creation cannot be bound safely');
    }
    if (tokenEntry && readFileSync(tokenPath, 'utf8').trim() !== stagedToken) {
      throw new Error('Reviewer staged token does not match local state');
    }
    const agentId = reviewerAgentId(agents[0]);
    const reboundToken = parseAgentToken(run(passCli, [
      'agent', 'renew', '--expiration', '3m', '--output', 'json', REVIEWER_AGENT,
    ]));
    await tokenAuthenticator(passCli, reboundToken);
    writeAtomic(tokenPath, reboundToken, null, 0o600);
    ownership = writeReviewerOwnership(
      ownershipPath, agentId, 'provisioning', emptyReviewerItems(),
    );
    rmSync(stagingPath, { force: true });
    recovered = true;
  } else if (agents.length === 0) {
    if (ownership || tokenEntry) {
      throw new Error('Reviewer local credential state has no matching agent');
    }
    createRunner(passCli, [
      'agent', 'create', REVIEWER_AGENT, '--expiration', '3m',
    ], stagingPath);
    readReviewerCreation(stagingPath);
    agents = parseValueFreeList(
      run(passCli, ['agent', 'list', '--output', 'json']), 'agent list', 'agents',
    ).filter((agent) => agent?.name === REVIEWER_AGENT);
    if (agents.length !== 1) {
      throw new Error('Created reviewer agent could not be identified uniquely');
    }
    const agentId = reviewerAgentId(agents[0]);
    const reboundToken = parseAgentToken(run(passCli, [
      'agent', 'renew', '--expiration', '3m', '--output', 'json', REVIEWER_AGENT,
    ]));
    await tokenAuthenticator(passCli, reboundToken);
    writeAtomic(tokenPath, reboundToken, null, 0o600);
    ownership = writeReviewerOwnership(
      ownershipPath, agentId, 'provisioning', emptyReviewerItems(),
    );
    rmSync(stagingPath, { force: true });
    created = true;
  } else {
    const agentId = reviewerAgentId(agents[0]);
    if (!tokenEntry) throw new Error('Reviewer token file is missing');
    if (!ownership) {
      const legacyShareId = reviewerLegacyShare(listReviewerAccess(passCli, run));
      if (legacyShareId === null) {
        throw new Error('Reviewer agent exists without a matching ownership record');
      }
      ownership = writeReviewerOwnership(
        ownershipPath, agentId, 'provisioning', emptyReviewerItems(), legacyShareId,
      );
      migrated = true;
    } else if (ownership.agent_id !== agentId) {
      throw new Error('Reviewer agent does not match its ownership record');
    }
  }

  const expireTime = Number(agents[0]?.expire_time);
  const expired = Number.isFinite(expireTime) && expireTime <= Math.floor(now() / 1000);
  let renewed = false;
  if (expired) {
    const response = run(passCli, [
      'agent', 'renew', '--expiration', '3m', '--output', 'json', REVIEWER_AGENT,
    ]);
    writeAtomic(tokenPath, parseAgentToken(response), null, 0o600);
    renewed = true;
  }

  const grants = listReviewerAccess(passCli, run);
  const legacyMayAlreadyBeRevoked = ownership.legacy_share_id !== null
    && reviewerLocatorsComplete(ownership.items);
  const currentItems = inspectReviewerAccess(
    grants, expectedItemIds, ownership.legacy_share_id, legacyMayAlreadyBeRevoked,
  );
  assertSavedReviewerLocators(ownership, currentItems);
  const complete = reviewerLocatorsComplete(currentItems);
  if (ownership.state === 'ready' && ownership.legacy_share_id === null && complete) {
    await verifyReviewerFields({ home, fieldReader, items: currentItems });
  } else {
    await reconcileReviewerGrants({
      home, passCli, run, fieldReader, ownershipPath, ownership, expectedItemIds,
    });
  }
  return { created, renewed, migrated, recovered };
}

function context7GrantDigest() {
  return createHash('sha256').update(JSON.stringify({
    vault: CONTEXT7_VAULT,
    item: CONTEXT7_ITEM,
    role: 'viewer',
  })).digest('hex');
}

function context7AgentId(agent) {
  const id = agent?.pat_id ?? agent?.id;
  if (!['string', 'number'].includes(typeof id) || String(id).length === 0) {
    throw new Error('Proton agent list returned an invalid Context7 identity');
  }
  return String(id);
}

function context7Ownership(agentId, state, access = null) {
  return {
    version: CONTEXT7_STATE_VERSION,
    agent_id: agentId,
    agent_name: CONTEXT7_AGENT,
    state,
    share_id: access?.shareId ?? null,
    item_id: access?.itemId ?? null,
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
  const legacyKeys = ['agent_id', 'agent_name', 'grant_sha256', 'state', 'version'];
  if (JSON.stringify(keys) === JSON.stringify(legacyKeys)
      && document.version === 1
      && document.agent_name === CONTEXT7_AGENT
      && typeof document.agent_id === 'string'
      && document.agent_id.length > 0
      && ['creating', 'ready'].includes(document.state)
      && document.grant_sha256 === context7GrantDigest()) {
    return { ...document, version: CONTEXT7_STATE_VERSION, state: 'creating',
      share_id: null, item_id: null };
  }
  const expectedKeys = [
    'agent_id', 'agent_name', 'grant_sha256', 'item_id', 'share_id', 'state', 'version',
  ];
  const completeAccess = typeof document?.share_id === 'string'
    && document.share_id.length > 0
    && typeof document.item_id === 'string'
    && document.item_id.length > 0;
  const pendingAccess = document?.state === 'creating'
    && document.share_id === null
    && document.item_id === null;
  if (JSON.stringify(keys) !== JSON.stringify(expectedKeys)
      || document.version !== CONTEXT7_STATE_VERSION
      || typeof document.agent_id !== 'string'
      || document.agent_id.length === 0
      || document.agent_name !== CONTEXT7_AGENT
      || !['creating', 'ready'].includes(document.state)
      || (!completeAccess && !pendingAccess)
      || document.grant_sha256 !== context7GrantDigest()) {
    throw new Error('Context7 ownership record is invalid');
  }
  return document;
}

function writeContext7Ownership(path, agentId, state, access = null) {
  writeAtomic(path, `${JSON.stringify(context7Ownership(agentId, state, access))}\n`, null, 0o600);
}

async function verifyContext7Field({ home, fieldReader, access }) {
  await fieldReader({
    tokenFile: CONTEXT7_TOKEN_FILE,
    shareId: access.shareId,
    itemId: access.itemId,
    field: CONTEXT7_FIELD,
    reason: 'Verify Discord Ferry Context7 access',
    home,
  });
}

function readContext7AccessGrant(passCli, run) {
  const grants = parseValueFreeList(run(passCli, [
    'personal-access-token', 'access', 'list-access',
    '--personal-access-token-name', CONTEXT7_AGENT,
    '--output', 'json',
  ]), 'access list', 'accesses');
  if (grants.length !== 1) {
    throw new Error('Context7 agent must have exactly one access grant');
  }
  const grant = grants[0];
  if (grant?.type !== 'item'
      || grant.item_title !== CONTEXT7_ITEM
      || grant.role !== 'Viewer'
      || typeof grant.share_id !== 'string'
      || grant.share_id.length === 0
      || typeof grant.item_id !== 'string'
      || grant.item_id.length === 0) {
    throw new Error('Context7 agent access grant is invalid');
  }
  return { shareId: grant.share_id, itemId: grant.item_id };
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
  const created = run(passCli, [
    'agent', 'create', CONTEXT7_AGENT, '--expiration', '3m',
  ]);
  const token = parseAgentToken(created);
  writeContext7Ownership(ownershipPath, CONTEXT7_PENDING_AGENT_ID, 'creating');
  writeAtomic(tokenPath, token, null, 0o600);
  const agents = parseValueFreeList(
    run(passCli, ['agent', 'list', '--output', 'json']),
    'agent list',
    'agents',
  ).filter((agent) => agent?.name === CONTEXT7_AGENT);
  if (agents.length !== 1) {
    throw new Error('created Context7 agent could not be identified uniquely');
  }
  const agentId = context7AgentId(agents[0]);
  writeContext7Ownership(ownershipPath, agentId, 'creating');
  run(passCli, [
    'agent', 'access', 'grant', CONTEXT7_AGENT,
    '--vault-name', CONTEXT7_VAULT,
    '--item-title', CONTEXT7_ITEM,
    '--role', 'viewer',
  ]);
  const access = readContext7AccessGrant(passCli, run);
  await verifyContext7Field({ home, fieldReader, access });
  writeContext7Ownership(ownershipPath, agentId, 'ready', access);
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
    const agentId = context7AgentId(agent);
    const pendingCreation = ownership?.state === 'creating'
      && ownership.agent_id === CONTEXT7_PENDING_AGENT_ID;
    if (!ownership || (!pendingCreation && ownership.agent_id !== agentId)) {
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
      await verifyContext7Field({ home, fieldReader, access: {
        shareId: ownership.share_id,
        itemId: ownership.item_id,
      } });
      return { created: false, renewed: true, recovered: false };
    }
    await verifyContext7Field({ home, fieldReader, access: {
      shareId: ownership.share_id,
      itemId: ownership.item_id,
    } });
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
