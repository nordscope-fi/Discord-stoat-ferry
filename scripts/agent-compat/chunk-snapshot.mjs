#!/usr/bin/env node

import { createHash, randomUUID } from 'node:crypto';
import { spawnSync } from 'node:child_process';
import {
  closeSync,
  existsSync,
  lstatSync,
  mkdirSync,
  mkdtempSync,
  openSync,
  readFileSync,
  realpathSync,
  renameSync,
  rmSync,
  unlinkSync,
  writeFileSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { basename, dirname, isAbsolute, relative, resolve, sep } from 'node:path';
import { pathToFileURL } from 'node:url';

export const SCHEMA = 'ferry.chunk-snapshot/v1';

const PATHS_SCHEMA = 'ferry.chunk-snapshot/paths-v1';
const DEFAULT_EVIDENCE_ROOT = 'docs/plans/.review/chunk-snapshots';
const PRIVATE_SUFFIX = /\.(?:key|p12|pfx|pem)$/iu;
const HOST_CONFIGS = new Set([
  '.codex/config.toml',
  '.mcp.json',
  '.qwen/settings.json',
  '.vibe/config.toml',
  'CLAUDE.md',
  'QWEN.md',
]);

class ArgumentError extends Error {}
class SnapshotError extends Error {}

export function sha256(value) {
  return createHash('sha256').update(value).digest('hex');
}

export function stableJson(value) {
  const sort = item => {
    if (item === null || typeof item === 'string' || typeof item === 'boolean') return item;
    if (typeof item === 'number' && Number.isFinite(item)) return item;
    if (Array.isArray(item)) return item.map(sort);
    if (item && typeof item === 'object' && Object.getPrototypeOf(item) === Object.prototype) {
      return Object.fromEntries(Object.keys(item).sort().map(key => [key, sort(item[key])]));
    }
    throw new TypeError('snapshot records contain JSON values only');
  };
  return `${JSON.stringify(sort(value))}\n`;
}

function recordDigest(record) {
  const { digest_sha256: _ignored, ...unsigned } = record;
  return sha256(Buffer.from(stableJson(unsigned)));
}

function finishRecord(record) {
  return { ...record, digest_sha256: recordDigest(record) };
}

function isObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function pathMetadata(candidate) {
  try {
    return lstatSync(candidate);
  } catch (error) {
    if (error?.code === 'ENOENT') return null;
    throw error;
  }
}

function canonicalRoot(root) {
  if (typeof root !== 'string' || root.length === 0) {
    throw new ArgumentError('root is required');
  }
  const canonical = realpathSync(root);
  if (!lstatSync(canonical).isDirectory()) throw new ArgumentError('root must be a directory');
  return canonical;
}

function inside(root, candidate, allowRoot = false) {
  const offset = relative(root, candidate);
  if (offset === '') return allowRoot;
  return offset !== '..' && !offset.startsWith(`..${sep}`) && !isAbsolute(offset);
}

function normalizeLogicalPath(logicalPath, evidenceRoot = DEFAULT_EVIDENCE_ROOT) {
  if (
    typeof logicalPath !== 'string'
    || logicalPath.length === 0
    || isAbsolute(logicalPath)
    || logicalPath.includes('\\')
    || /[\0-\x1f\x7f]/u.test(logicalPath)
  ) {
    throw new SnapshotError('candidate path must be a clean relative path');
  }
  const segments = logicalPath.split('/');
  if (segments.some(segment => segment === '' || segment === '.' || segment === '..')) {
    throw new SnapshotError(`rejected before read: ${logicalPath}`);
  }
  const normalizedEvidence = evidenceRoot.replace(/\/$/u, '');
  const name = basename(logicalPath);
  if (
    segments.includes('.git')
    || logicalPath === normalizedEvidence
    || logicalPath.startsWith(`${normalizedEvidence}/`)
    || HOST_CONFIGS.has(logicalPath)
    || /^\.env(?:\.|$)/u.test(name)
    || PRIVATE_SUFFIX.test(name)
    || (logicalPath.startsWith('.claude/') && /settings.*\.json$/u.test(name))
  ) {
    throw new SnapshotError(`rejected before read: ${logicalPath}`);
  }
  return segments.join('/');
}

function validateUniquePaths(paths, evidenceRoot) {
  if (!Array.isArray(paths) || paths.length === 0) {
    throw new ArgumentError('approved paths must be a non-empty array');
  }
  const normalized = paths.map(path => normalizeLogicalPath(path, evidenceRoot));
  if (new Set(normalized).size !== normalized.length) {
    throw new ArgumentError('approved paths must not repeat');
  }
  return normalized;
}

function normalizeEvidenceRoot(localPath) {
  if (
    typeof localPath !== 'string'
    || localPath.length === 0
    || isAbsolute(localPath)
    || localPath.includes('\\')
    || /[\0-\x1f\x7f]/u.test(localPath)
  ) {
    throw new SnapshotError('evidence root must be a clean relative path');
  }
  const segments = localPath.split('/');
  if (
    segments.some(segment => segment === '' || segment === '.' || segment === '..')
    || segments.includes('.git')
  ) {
    throw new SnapshotError('evidence root must be a clean relative path');
  }
  return segments.join('/');
}

function ensureDirectory(root, localPath) {
  const normalized = normalizeEvidenceRoot(localPath);
  const segments = normalized.split('/');
  let current = root;
  for (const segment of segments) {
    current = resolve(current, segment);
    const metadata = pathMetadata(current);
    if (metadata !== null) {
      if (metadata.isSymbolicLink() || !metadata.isDirectory()) {
        throw new SnapshotError(`evidence directory is unsafe: ${normalized}`);
      }
    } else {
      mkdirSync(current, { mode: 0o700 });
    }
  }
  if (!inside(root, realpathSync(current))) {
    throw new SnapshotError(`evidence directory leaves checkout: ${normalized}`);
  }
  return current;
}

function resolveExistingFile(root, localPath, purpose) {
  if (typeof localPath !== 'string' || localPath.length === 0 || isAbsolute(localPath)) {
    throw new ArgumentError(`${purpose} path must be relative to the checkout`);
  }
  const candidate = resolve(root, localPath);
  const metadata = pathMetadata(candidate);
  if (!inside(root, candidate) || metadata === null) {
    throw new SnapshotError(`${purpose} file does not exist`);
  }
  if (metadata.isSymbolicLink() || !metadata.isFile() || !inside(root, realpathSync(candidate))) {
    throw new SnapshotError(`${purpose} path must name a regular checkout file`);
  }
  return candidate;
}

function resolveEvidencePath(root, evidenceRoot, localPath, purpose, mustExist = true) {
  if (typeof localPath !== 'string' || localPath.length === 0 || isAbsolute(localPath)) {
    throw new ArgumentError(`${purpose} path must be relative to the checkout`);
  }
  const evidenceDirectory = resolve(root, evidenceRoot);
  const candidate = resolve(root, localPath);
  if (!inside(evidenceDirectory, candidate)) {
    throw new SnapshotError(`${purpose} path leaves snapshot evidence`);
  }
  if (mustExist) {
    const metadata = pathMetadata(candidate);
    if (metadata === null) throw new SnapshotError(`${purpose} file does not exist`);
    if (
      metadata.isSymbolicLink()
      || !metadata.isFile()
      || !inside(realpathSync(evidenceDirectory), realpathSync(candidate))
    ) {
      throw new SnapshotError(`${purpose} path must name a regular file`);
    }
  }
  return candidate;
}

function verifiedPlan(root, planPath, approvedPlanDigestPath, evidenceRoot) {
  const planFile = resolveExistingFile(root, planPath, 'plan');
  const digestFile = resolveEvidencePath(
    root,
    evidenceRoot,
    approvedPlanDigestPath,
    'approved plan digest',
  );
  const digestText = readFileSync(digestFile, 'utf8');
  const match = /^([a-f0-9]{64})  ([^\n]+)\n?$/u.exec(digestText);
  if (!match || match[2] !== planPath) {
    throw new SnapshotError('approved plan digest file is malformed or names another plan');
  }
  const actual = sha256(readFileSync(planFile));
  if (actual !== match[1]) throw new SnapshotError('approved plan digest changed');
  return { path: planPath, sha256: actual };
}

function positiveIssue(value, name) {
  if (!Number.isSafeInteger(value) || value <= 0) {
    throw new ArgumentError(`${name} must be a positive integer`);
  }
  return value;
}

function runGit(root, args, { env = {}, input = undefined, encoding = 'utf8' } = {}) {
  const result = spawnSync('git', args, {
    cwd: root,
    env: { ...process.env, ...env },
    input,
    encoding,
    maxBuffer: 64 * 1024 * 1024,
  });
  if (result.status !== 0) throw new SnapshotError(`git ${args[0]} failed`);
  return result.stdout;
}

function verifiedStartingCommit(root, startingCommit) {
  if (typeof startingCommit !== 'string' || !/^[a-f0-9]{40}$/u.test(startingCommit)) {
    throw new ArgumentError('starting commit must be a full lower-case object id');
  }
  runGit(root, ['cat-file', '-e', `${startingCommit}^{commit}`]);
  return startingCommit;
}

function blobPath(digest) {
  return `blobs/${digest}.bin`;
}

function saveBlob(root, evidenceRoot, bytes) {
  const digest = sha256(bytes);
  const local = blobPath(digest);
  const directory = ensureDirectory(root, `${evidenceRoot}/blobs`);
  const destination = resolve(directory, `${digest}.bin`);
  const existing = pathMetadata(destination);
  if (existing !== null) {
    const metadata = existing;
    if (
      metadata.isSymbolicLink()
      || !metadata.isFile()
      || sha256(readFileSync(destination)) !== digest
    ) {
      throw new SnapshotError(`stored blob is invalid: ${digest}`);
    }
    return local;
  }
  let descriptor;
  try {
    descriptor = openSync(destination, 'wx', 0o600);
    writeFileSync(descriptor, bytes);
  } finally {
    if (descriptor !== undefined) closeSync(descriptor);
  }
  if (sha256(readFileSync(destination)) !== digest) {
    throw new SnapshotError(`stored blob verification failed: ${digest}`);
  }
  return local;
}

function absentState(logicalPath, domain = 'git') {
  return {
    path: logicalPath,
    domain,
    exists: false,
    mode: null,
    sha256: null,
    blob: null,
  };
}

function presentState(root, evidenceRoot, logicalPath, mode, bytes, domain = 'git') {
  const digest = sha256(bytes);
  return {
    path: logicalPath,
    domain,
    exists: true,
    mode,
    sha256: digest,
    blob: saveBlob(root, evidenceRoot, bytes),
  };
}

function safeWorkingPath(root, logicalPath) {
  const segments = logicalPath.split('/');
  let current = root;
  for (const segment of segments.slice(0, -1)) {
    current = resolve(current, segment);
    const metadata = pathMetadata(current);
    if (metadata === null) return resolve(root, logicalPath);
    if (metadata.isSymbolicLink() || !metadata.isDirectory()) {
      throw new SnapshotError(`rejected before read: ${logicalPath}`);
    }
  }
  const target = resolve(root, logicalPath);
  if (!inside(root, target)) throw new SnapshotError(`rejected before read: ${logicalPath}`);
  return target;
}

export function capturePathState({
  root,
  logicalPath,
  domain,
  primaryRoot = null,
  evidenceRoot = DEFAULT_EVIDENCE_ROOT,
}) {
  const checkout = canonicalRoot(root);
  const normalized = normalizeLogicalPath(logicalPath, evidenceRoot);
  if (domain !== 'git' || (primaryRoot !== null && realpathSync(primaryRoot) !== checkout)) {
    throw new SnapshotError('Task 1 captures checkout Git paths only');
  }
  const target = safeWorkingPath(checkout, normalized);
  const metadata = pathMetadata(target);
  if (metadata === null) return { state: absentState(normalized), bytes: null };
  if (metadata.isSymbolicLink() || !metadata.isFile()) {
    throw new SnapshotError(`rejected before read: ${normalized}`);
  }
  let bytes;
  try {
    bytes = readFileSync(target);
  } catch {
    throw new SnapshotError(`unreadable path ${normalized}`);
  }
  const mode = (metadata.mode & 0o111) === 0 ? '100644' : '100755';
  return {
    state: presentState(checkout, evidenceRoot, normalized, mode, bytes),
    bytes,
  };
}

function gitTreeState(root, evidenceRoot, startingCommit, logicalPath) {
  const output = runGit(root, ['ls-tree', '-z', startingCommit, '--', logicalPath], {
    encoding: null,
  });
  if (output.length === 0) return absentState(logicalPath);
  const text = output.subarray(0, output.length - 1).toString('utf8');
  const match = /^(100644|100755) blob ([a-f0-9]{40,64})\t(.+)$/u.exec(text);
  if (!match || match[3] !== logicalPath) {
    throw new SnapshotError(`unsupported Git entry: ${logicalPath}`);
  }
  const bytes = runGit(root, ['cat-file', 'blob', match[2]], { encoding: null });
  return presentState(root, evidenceRoot, logicalPath, match[1], bytes);
}

function stateIdentity(state) {
  return stableJson({
    path: state.path,
    domain: state.domain,
    exists: state.exists,
    mode: state.mode,
    sha256: state.sha256,
    blob: state.blob,
  });
}

function statesEqual(left, right) {
  return stateIdentity(left) === stateIdentity(right);
}

function parseWorktreeChanges(root) {
  const output = runGit(
    root,
    ['status', '--porcelain=v1', '-z', '--untracked-files=all', '--ignored=no'],
  );
  const entries = output.split('\0').filter(value => value.length > 0);
  const paths = [];
  for (let index = 0; index < entries.length; index += 1) {
    const entry = entries[index];
    if (entry.length < 4) throw new SnapshotError('Git status output is malformed');
    const status = entry.slice(0, 2);
    if (status.includes('R') || status.includes('C')) {
      throw new SnapshotError('renamed candidate paths are not supported');
    }
    paths.push(entry.slice(3));
  }
  return paths;
}

function assertWorktreeChangesAllowed(root, evidenceRoot, approvedPaths) {
  const approved = new Set(approvedPaths);
  for (const changed of parseWorktreeChanges(root)) {
    if (changed === evidenceRoot || changed.startsWith(`${evidenceRoot}/`)) continue;
    if (!approved.has(changed)) {
      throw new SnapshotError(`unrelated checkout change: ${changed}`);
    }
  }
}

function recordPath(evidenceRoot, kind, identifier, filename) {
  return `${evidenceRoot}/${kind}/${identifier}/${filename}`;
}

function publishRecord(root, evidenceRoot, localPath, record) {
  const destination = resolveEvidencePath(root, evidenceRoot, localPath, 'record output', false);
  ensureDirectory(root, relative(root, dirname(destination)).split(sep).join('/'));
  const content = stableJson(record);
  const existing = pathMetadata(destination);
  if (existing !== null) {
    const metadata = existing;
    if (metadata.isSymbolicLink() || !metadata.isFile()) {
      throw new SnapshotError(`record output is unsafe: ${localPath}`);
    }
    if (readFileSync(destination, 'utf8') !== content) {
      throw new SnapshotError(`immutable record already exists: ${localPath}`);
    }
    return;
  }
  const temporary = resolve(dirname(destination), `.record.${process.pid}.${randomUUID()}.tmp`);
  try {
    writeFileSync(temporary, content, { encoding: 'utf8', flag: 'wx', mode: 0o600 });
    if (readFileSync(temporary, 'utf8') !== content) {
      throw new SnapshotError(`record staging verification failed: ${localPath}`);
    }
    renameSync(temporary, destination);
  } finally {
    if (existsSync(temporary)) unlinkSync(temporary);
  }
}

function readRecord(root, evidenceRoot, localPath, expectedKind) {
  const file = resolveEvidencePath(root, evidenceRoot, localPath, expectedKind);
  let record;
  try {
    record = JSON.parse(readFileSync(file, 'utf8'));
  } catch {
    throw new SnapshotError(`${expectedKind} record is not valid JSON`);
  }
  if (
    !isObject(record)
    || record.schema !== SCHEMA
    || record.kind !== expectedKind
    || record.digest_sha256 !== recordDigest(record)
  ) {
    throw new SnapshotError(`${expectedKind} record is invalid`);
  }
  return record;
}

function validGitState(state, evidenceRoot) {
  if (!isObject(state) || state.domain !== 'git' || typeof state.exists !== 'boolean') {
    return false;
  }
  try {
    if (normalizeLogicalPath(state.path, evidenceRoot) !== state.path) return false;
  } catch {
    return false;
  }
  if (!state.exists) {
    return state.mode === null && state.sha256 === null && state.blob === null;
  }
  return (
    (state.mode === '100644' || state.mode === '100755')
    && /^[a-f0-9]{64}$/u.test(state.sha256)
    && state.blob === blobPath(state.sha256)
  );
}

function validatePriorTaskRecord(record, context) {
  if (
    record.plan_path !== context.plan.path
    || record.approved_plan_sha256 !== context.plan.sha256
    || record.starting_commit !== context.startingCommit
    || !Number.isSafeInteger(record.task_issue)
    || record.task_issue <= 0
    || !Number.isSafeInteger(record.chunk_issue)
    || record.chunk_issue <= 0
    || typeof record.baseline_digest !== 'string'
    || !/^[a-f0-9]{64}$/u.test(record.baseline_digest)
    || !Array.isArray(record.operations)
    || record.operations.length === 0
  ) {
    throw new SnapshotError('prior task record identity is invalid');
  }
  for (const operation of record.operations) {
    if (
      !isObject(operation)
      || !validGitState(operation.before, context.evidenceRoot)
      || !validGitState(operation.after, context.evidenceRoot)
      || operation.before.path !== operation.after.path
    ) {
      throw new SnapshotError('prior task record operation is invalid');
    }
  }
}

function loadPriorTaskRecords(context, paths) {
  if (!Array.isArray(paths)) {
    throw new ArgumentError('prior task records must be an array');
  }
  const records = paths.map(path => readRecord(
    context.root,
    context.evidenceRoot,
    path,
    'task-patch',
  ));
  for (const record of records) validatePriorTaskRecord(record, context);
  const taskIssues = records.map(record => record.task_issue);
  if (taskIssues.some((issue, index) => (
    issue >= context.taskIssue || (index > 0 && issue <= taskIssues[index - 1])
  ))) {
    throw new SnapshotError('prior task records are out of order');
  }
  if (records.length > 0) {
    materializeGitStates({
      root: context.root,
      startingCommit: context.startingCommit,
      operations: records.flatMap(record => record.operations),
      evidenceRoot: context.evidenceRoot,
    });
  }
  return records;
}

function commonRecord({ plan, startingCommit, chunkIssue, taskIssue = null }) {
  return {
    schema: SCHEMA,
    plan_path: plan.path,
    approved_plan_sha256: plan.sha256,
    starting_commit: startingCommit,
    chunk_issue: chunkIssue,
    ...(taskIssue === null ? {} : { task_issue: taskIssue }),
  };
}

function baselineRecord(options, states, bootstrap = null) {
  return finishRecord({
    ...commonRecord(options),
    kind: 'task-baseline',
    approved_paths: states.map(state => state.path),
    path_states: states,
    instruction_path_states: [],
    prior_task_digests: options.priorTaskRecords.map(record => record.digest_sha256),
    ...(bootstrap === null ? {} : { bootstrap }),
  });
}

function captureContext(options) {
  const root = canonicalRoot(options.root);
  const evidenceRoot = normalizeEvidenceRoot(options.evidenceRoot ?? DEFAULT_EVIDENCE_ROOT);
  ensureDirectory(root, evidenceRoot);
  const plan = verifiedPlan(
    root,
    options.planPath,
    options.approvedPlanDigestPath,
    evidenceRoot,
  );
  const startingCommit = verifiedStartingCommit(root, options.startingCommit);
  const chunkIssue = positiveIssue(options.chunkIssue, 'chunk issue');
  const taskIssue = positiveIssue(options.taskIssue, 'task issue');
  const paths = validateUniquePaths(options.paths, evidenceRoot);
  if ((options.instructionPaths ?? []).length !== 0) {
    throw new SnapshotError('instruction paths are added in Task 2');
  }
  const context = {
    root,
    evidenceRoot,
    plan,
    startingCommit,
    chunkIssue,
    taskIssue,
    paths,
  };
  const priorTaskRecords = loadPriorTaskRecords(context, options.priorTaskRecords ?? []);
  return { ...context, priorTaskRecords };
}

function stateAfterPriorTasks(context, logicalPath) {
  let state = gitTreeState(
    context.root,
    context.evidenceRoot,
    context.startingCommit,
    logicalPath,
  );
  for (const record of context.priorTaskRecords) {
    for (const operation of record.operations) {
      if (operation.after.path === logicalPath) state = operation.after;
    }
  }
  return state;
}

export function captureBaseline(options) {
  const context = captureContext(options);
  assertWorktreeChangesAllowed(context.root, context.evidenceRoot, context.paths);
  const states = context.paths.map(logicalPath => {
    const before = stateAfterPriorTasks(context, logicalPath);
    const current = capturePathState({
      root: context.root,
      logicalPath,
      domain: 'git',
      evidenceRoot: context.evidenceRoot,
    }).state;
    if (!statesEqual(before, current)) {
      const source = context.priorTaskRecords.length === 0 ? 'starting commit' : 'prior tasks';
      throw new SnapshotError(`baseline path does not match ${source}: ${logicalPath}`);
    }
    return before;
  });
  const record = baselineRecord(context, states);
  const path = recordPath(context.evidenceRoot, 'tasks', context.taskIssue, 'baseline.json');
  publishRecord(context.root, context.evidenceRoot, path, record);
  return { path, record };
}

function importBootstrap(options) {
  const context = captureContext(options);
  const headFile = resolveEvidencePath(
    context.root,
    context.evidenceRoot,
    options.headFile,
    'bootstrap head',
  );
  const statusFile = resolveEvidencePath(
    context.root,
    context.evidenceRoot,
    options.statusFile,
    'bootstrap status',
  );
  const headBytes = readFileSync(headFile);
  const statusBytes = readFileSync(statusFile);
  if (headBytes.toString('utf8').trim() !== context.startingCommit) {
    throw new SnapshotError('bootstrap head does not match starting commit');
  }
  const savedStatus = statusBytes.toString('utf8');
  if (savedStatus.split('\n').some(line => line.length > 0 && !line.startsWith('# '))) {
    throw new SnapshotError('bootstrap status was not clean');
  }
  const currentHead = runGit(context.root, ['rev-parse', 'HEAD']).trim();
  if (currentHead !== context.startingCommit) {
    throw new SnapshotError('checkout head no longer matches bootstrap');
  }
  assertWorktreeChangesAllowed(context.root, context.evidenceRoot, context.paths);
  const states = context.paths.map(logicalPath => {
    const before = gitTreeState(
      context.root,
      context.evidenceRoot,
      context.startingCommit,
      logicalPath,
    );
    if (before.exists) {
      throw new SnapshotError(`bootstrap path existed at starting commit: ${logicalPath}`);
    }
    return before;
  });
  const record = baselineRecord(context, states, {
    head_sha256: sha256(headBytes),
    status_sha256: sha256(statusBytes),
  });
  const path = recordPath(context.evidenceRoot, 'tasks', context.taskIssue, 'baseline.json');
  publishRecord(context.root, context.evidenceRoot, path, record);
  return { path, record };
}

function assertRecordIdentity(record, context) {
  if (
    record.plan_path !== context.plan.path
    || record.approved_plan_sha256 !== context.plan.sha256
    || record.starting_commit !== context.startingCommit
    || record.chunk_issue !== context.chunkIssue
    || record.task_issue !== context.taskIssue
  ) {
    throw new SnapshotError('record identity does not match capture request');
  }
}

export function captureTask(options) {
  const context = captureContext(options);
  assertWorktreeChangesAllowed(context.root, context.evidenceRoot, context.paths);
  const baseline = readRecord(
    context.root,
    context.evidenceRoot,
    options.baselinePath,
    'task-baseline',
  );
  assertRecordIdentity(baseline, context);
  if (stableJson(baseline.approved_paths) !== stableJson(context.paths)) {
    throw new SnapshotError('baseline approved paths changed');
  }
  if (
    stableJson(baseline.prior_task_digests)
    !== stableJson(context.priorTaskRecords.map(record => record.digest_sha256))
  ) {
    throw new SnapshotError('baseline prior task records changed');
  }
  const operations = [];
  for (const before of baseline.path_states) {
    let after;
    try {
      after = capturePathState({
        root: context.root,
        logicalPath: before.path,
        domain: 'git',
        evidenceRoot: context.evidenceRoot,
      }).state;
    } catch (error) {
      throw new SnapshotError(`chunk ${context.chunkIssue}: ${error.message}`);
    }
    if (!statesEqual(before, after)) operations.push({ before, after });
  }
  if (operations.length === 0) throw new SnapshotError('task patch has no changed operation');
  const record = finishRecord({
    ...commonRecord(context),
    kind: 'task-patch',
    baseline_digest: baseline.digest_sha256,
    operations,
  });
  const path = options.outputPath
    ?? recordPath(context.evidenceRoot, 'tasks', context.taskIssue, 'patch.json');
  publishRecord(context.root, context.evidenceRoot, path, record);
  return { path, record };
}

function rawWorkingIdentity(root, logicalPath) {
  const target = safeWorkingPath(root, logicalPath);
  const metadata = pathMetadata(target);
  if (metadata === null) return stableJson(absentState(logicalPath));
  if (metadata.isSymbolicLink() || !metadata.isFile()) {
    throw new SnapshotError(`rejected before read: ${logicalPath}`);
  }
  const bytes = readFileSync(target);
  return stableJson({
    ...absentState(logicalPath),
    exists: true,
    mode: (metadata.mode & 0o111) === 0 ? '100644' : '100755',
    sha256: sha256(bytes),
    blob: blobPath(sha256(bytes)),
  });
}

function activeIndexIdentity(root) {
  const value = runGit(root, ['rev-parse', '--git-path', 'index']).trim();
  const indexPath = isAbsolute(value) ? value : resolve(root, value);
  return existsSync(indexPath) ? sha256(readFileSync(indexPath)) : null;
}

function indexState(root, evidenceRoot, indexPath, logicalPath) {
  const output = runGit(root, ['ls-files', '-s', '-z', '--', logicalPath], {
    env: { GIT_INDEX_FILE: indexPath },
    encoding: null,
  });
  if (output.length === 0) return absentState(logicalPath);
  const text = output.subarray(0, output.length - 1).toString('utf8');
  const match = /^(100644|100755) ([a-f0-9]{40,64}) 0\t(.+)$/u.exec(text);
  if (!match || match[3] !== logicalPath) {
    throw new SnapshotError(`temporary index entry is invalid: ${logicalPath}`);
  }
  const bytes = runGit(root, ['cat-file', 'blob', match[2]], { encoding: null });
  return presentState(root, evidenceRoot, logicalPath, match[1], bytes);
}

function checkedBlob(root, evidenceRoot, state) {
  const file = resolveEvidencePath(
    root,
    evidenceRoot,
    `${evidenceRoot}/${state.blob}`,
    'content blob',
  );
  const bytes = readFileSync(file);
  if (sha256(bytes) !== state.sha256) throw new SnapshotError(`blob changed: ${state.path}`);
  return bytes;
}

function materializeGitStates({ root, startingCommit, operations, evidenceRoot }) {
  const touched = [...new Set(operations.map(operation => operation.after.path))];
  const indexBefore = activeIndexIdentity(root);
  const worktreeBefore = touched.map(path => rawWorkingIdentity(root, path));
  const directory = mkdtempSync(resolve(tmpdir(), 'ferry-chunk-index-'));
  const indexPath = resolve(directory, 'index');
  try {
    const startingTree = runGit(root, ['rev-parse', `${startingCommit}^{tree}`]).trim();
    runGit(root, ['read-tree', startingTree], { env: { GIT_INDEX_FILE: indexPath } });
    for (const operation of operations) {
      const current = indexState(root, evidenceRoot, indexPath, operation.before.path);
      if (!statesEqual(current, operation.before)) {
        throw new SnapshotError(`operation before-state changed: ${operation.before.path}`);
      }
      if (!operation.after.exists) {
        runGit(root, ['update-index', '--force-remove', '--', operation.after.path], {
          env: { GIT_INDEX_FILE: indexPath },
        });
        continue;
      }
      const bytes = checkedBlob(root, evidenceRoot, operation.after);
      const objectId = runGit(root, ['hash-object', '-w', '--stdin'], {
        input: bytes,
        encoding: 'utf8',
      }).trim();
      runGit(
        root,
        [
          'update-index',
          '--add',
          '--cacheinfo',
          operation.after.mode,
          objectId,
          operation.after.path,
        ],
        { env: { GIT_INDEX_FILE: indexPath } },
      );
    }
    const endingTree = runGit(root, ['write-tree'], {
      env: { GIT_INDEX_FILE: indexPath },
    }).trim();
    return { startingTree, endingTree };
  } finally {
    rmSync(directory, { recursive: true, force: true });
    const indexAfter = activeIndexIdentity(root);
    const worktreeAfter = touched.map(path => rawWorkingIdentity(root, path));
    if (
      indexAfter !== indexBefore
      || stableJson(worktreeAfter) !== stableJson(worktreeBefore)
    ) {
      throw new SnapshotError('temporary materialization changed the active checkout');
    }
  }
}

export function captureChunk(options) {
  const root = canonicalRoot(options.root);
  const evidenceRoot = normalizeEvidenceRoot(options.evidenceRoot ?? DEFAULT_EVIDENCE_ROOT);
  ensureDirectory(root, evidenceRoot);
  const plan = verifiedPlan(
    root,
    options.planPath,
    options.approvedPlanDigestPath,
    evidenceRoot,
  );
  const chunkIssue = positiveIssue(options.chunkIssue, 'chunk issue');
  if ((options.priorSnapshotPaths ?? []).length !== 0) {
    throw new SnapshotError('prior snapshot reconstruction is added in Task 2');
  }
  if (!Array.isArray(options.taskRecords) || options.taskRecords.length === 0) {
    throw new ArgumentError('captureChunk requires ordered task records');
  }
  const taskRecords = options.taskRecords.map(path => readRecord(
    root,
    evidenceRoot,
    path,
    'task-patch',
  ));
  const taskIssues = taskRecords.map(record => record.task_issue);
  if (new Set(taskIssues).size !== taskIssues.length) {
    throw new SnapshotError('task records must not repeat');
  }
  const startingCommit = verifiedStartingCommit(
    root,
    options.startingCommit ?? taskRecords[0].starting_commit,
  );
  for (const record of taskRecords) {
    if (
      record.plan_path !== plan.path
      || record.approved_plan_sha256 !== plan.sha256
      || record.starting_commit !== startingCommit
      || record.chunk_issue !== chunkIssue
    ) {
      throw new SnapshotError('task record does not belong to this chunk');
    }
  }
  const operations = taskRecords.flatMap(record => record.operations);
  const { startingTree, endingTree } = materializeGitStates({
    root,
    startingCommit,
    operations,
    evidenceRoot,
  });
  const finalStates = new Map();
  for (const operation of operations) finalStates.set(operation.after.path, operation.after);
  for (const state of finalStates.values()) {
    const current = capturePathState({
      root,
      logicalPath: state.path,
      domain: 'git',
      evidenceRoot,
    }).state;
    if (!statesEqual(current, state)) {
      throw new SnapshotError(`candidate changed after task capture: ${state.path}`);
    }
  }
  const record = finishRecord({
    ...commonRecord({ plan, startingCommit, chunkIssue }),
    kind: 'chunk-snapshot',
    prior_snapshot_digests: [],
    task_issues: taskIssues,
    task_patch_digests: taskRecords.map(record => record.digest_sha256),
    approved_paths: [...finalStates.keys()],
    starting_git_tree: startingTree,
    ending_git_tree: endingTree,
    starting_instruction_states: [],
    ending_instruction_states: [],
  });
  const path = options.outputPath
    ?? recordPath(evidenceRoot, 'chunks', chunkIssue, 'snapshot.json');
  publishRecord(root, evidenceRoot, path, record);
  return { path, record };
}

function readPathsFile(root, evidenceRoot, localPath) {
  const file = resolveEvidencePath(root, evidenceRoot, localPath, 'paths');
  let value;
  try {
    value = JSON.parse(readFileSync(file, 'utf8'));
  } catch {
    throw new ArgumentError('paths file must contain valid JSON');
  }
  if (!isObject(value) || value.schema !== PATHS_SCHEMA || !Array.isArray(value.paths)) {
    throw new ArgumentError('paths file has the wrong schema');
  }
  return validateUniquePaths(value.paths, evidenceRoot);
}

function helpText() {
  return [
    'Usage: chunk-snapshot.mjs <command> --root <checkout> --plan <plan> --approved-plan-digest <file>',
    '',
    'Task 1 commands:',
    '  import-bootstrap  Import a verified clean pre-helper baseline',
    '  baseline          Capture a Git-derived task baseline',
    '  capture-task      Capture exact ordered task operations',
    '  capture-chunk     Capture ordered task patches and Git tree identities',
  ].join('\n');
}

function parseArgs(argv) {
  if (argv.length === 0 || argv[0] === '--help' || argv[0] === '-h') return { help: true };
  const command = argv[0];
  const commands = new Set(['import-bootstrap', 'baseline', 'capture-task', 'capture-chunk']);
  if (!commands.has(command)) throw new ArgumentError(`unknown command: ${command}`);
  const repeatable = new Set(['--prior-task-record', '--prior-snapshot', '--task']);
  const values = {};
  for (let index = 1; index < argv.length; index += 2) {
    const flag = argv[index];
    const value = argv[index + 1];
    if (!flag?.startsWith('--') || value === undefined || value.startsWith('--')) {
      throw new ArgumentError(`invalid argument near ${flag ?? '<end>'}`);
    }
    if (repeatable.has(flag)) {
      values[flag] ??= [];
      values[flag].push(value);
    } else {
      if (Object.hasOwn(values, flag)) throw new ArgumentError(`duplicate argument: ${flag}`);
      values[flag] = value;
    }
  }
  const allowed = {
    'import-bootstrap': new Set([
      '--root', '--plan', '--approved-plan-digest', '--chunk', '--parent', '--task', '--start',
      '--paths-file', '--head-file', '--status-file',
    ]),
    baseline: new Set([
      '--root', '--plan', '--approved-plan-digest', '--chunk', '--parent', '--task', '--start',
      '--paths-file', '--instruction-paths-file', '--prior-task-record',
    ]),
    'capture-task': new Set([
      '--root', '--plan', '--approved-plan-digest', '--chunk', '--parent', '--task', '--start',
      '--paths-file', '--instruction-paths-file', '--prior-task-record', '--baseline', '--output',
    ]),
    'capture-chunk': new Set([
      '--root', '--plan', '--approved-plan-digest', '--chunk', '--parent', '--task',
      '--prior-snapshot', '--output',
    ]),
  }[command];
  const unknown = Object.keys(values).find(flag => !allowed.has(flag));
  if (unknown) throw new ArgumentError(`unknown argument for ${command}: ${unknown}`);
  return { help: false, command, values };
}

function required(values, flag) {
  const value = values[flag];
  if (typeof value !== 'string' || value.length === 0) {
    throw new ArgumentError(`${flag} is required`);
  }
  return value;
}

function oneTask(values) {
  const tasks = values['--task'] ?? [];
  if (tasks.length !== 1) throw new ArgumentError('exactly one --task is required');
  return Number(tasks[0]);
}

function cliContext(values) {
  const root = canonicalRoot(required(values, '--root'));
  const evidenceRoot = DEFAULT_EVIDENCE_ROOT;
  return {
    root,
    evidenceRoot,
    planPath: required(values, '--plan'),
    approvedPlanDigestPath: required(values, '--approved-plan-digest'),
  };
}

function printResult(result) {
  process.stdout.write(stableJson({
    path: result.path,
    digest_sha256: result.record.digest_sha256,
  }));
}

export function main(argv = process.argv.slice(2)) {
  const parsed = parseArgs(argv);
  if (parsed.help) {
    process.stdout.write(`${helpText()}\n`);
    return;
  }
  const { command, values } = parsed;
  const common = cliContext(values);
  const chunkOrdinal = positiveIssue(Number(required(values, '--chunk')), 'chunk');
  const chunkIssue = positiveIssue(Number(required(values, '--parent')), 'parent issue');
  if (command === 'capture-chunk') {
    const taskIssues = values['--task'] ?? [];
    if (taskIssues.length === 0) throw new ArgumentError('at least one --task is required');
    const taskRecords = taskIssues.map(value => {
      const taskIssue = positiveIssue(Number(value), 'task issue');
      return recordPath(common.evidenceRoot, 'tasks', taskIssue, 'patch.json');
    });
    const first = readRecord(common.root, common.evidenceRoot, taskRecords[0], 'task-patch');
    const outputPath = values['--output']
      ?? recordPath(common.evidenceRoot, 'chunks', chunkOrdinal, 'snapshot.json');
    printResult(captureChunk({
      ...common,
      chunkIssue,
      startingCommit: first.starting_commit,
      priorSnapshotPaths: values['--prior-snapshot'] ?? [],
      taskRecords,
      outputPath,
    }));
    return;
  }
  const taskIssue = positiveIssue(oneTask(values), 'task issue');
  const startingCommit = required(values, '--start');
  const paths = readPathsFile(
    common.root,
    common.evidenceRoot,
    required(values, '--paths-file'),
  );
  const options = {
    ...common,
    chunkIssue,
    taskIssue,
    startingCommit,
    paths,
    instructionPaths: [],
    priorTaskRecords: values['--prior-task-record'] ?? [],
  };
  if (command === 'import-bootstrap') {
    printResult(importBootstrap({
      ...options,
      headFile: required(values, '--head-file'),
      statusFile: required(values, '--status-file'),
    }));
    return;
  }
  if (values['--instruction-paths-file']) {
    throw new SnapshotError('instruction paths are added in Task 2');
  }
  if (command === 'baseline') {
    printResult(captureBaseline(options));
    return;
  }
  printResult(captureTask({
    ...options,
    baselinePath: required(values, '--baseline'),
    outputPath: values['--output'] ?? null,
  }));
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  try {
    main(process.argv.slice(2));
  } catch (error) {
    process.stderr.write(`${error.message}\n`);
    process.exitCode = error instanceof ArgumentError ? 2 : 1;
  }
}
