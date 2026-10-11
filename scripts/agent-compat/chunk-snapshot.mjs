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
import {
  authorizeVerificationCommand,
  classifyVerification,
} from './review-verification.mjs';

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
const APPROVED_INSTRUCTION_PATHS = new Set([
  'AGENTS.md',
  '.claude/skills/df-chunk-review/SKILL.md',
  '.claude/skills/df-ship/SKILL.md',
  'docs/architecture/adr/040-review-chunk-snapshots-before-task-commits.md',
  'docs/architecture/adr/README.md',
]);
const CURRENT_RULE_PATHS = new Set([
  'AGENTS.md',
  '.claude/rules/accuracy.md',
  '.claude/rules/decision-accountability.md',
  '.claude/rules/right-sizing.md',
  '.claude/rules/agent-security.md',
  '.claude/rules/code-conventions.md',
  '.claude/rules/security.md',
  '.claude/rules/key-files.md',
  '.claude/rules/context7.md',
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
    throw new SnapshotError(`rejected before read: ${String(logicalPath)}`);
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
  const planBytes = readFileSync(planFile);
  const actual = sha256(planBytes);
  if (actual !== match[1]) throw new SnapshotError('approved plan digest changed');
  const source = planBytes.toString('utf8');
  const taskOrder = [...source.matchAll(/^### Task \d+:[^\n]*\(#(\d+)\)$/gmu)]
    .map(task => Number(task[1]));
  const chunkOrder = [...source.matchAll(/^## Chunk \d+ \(#(\d+)\):/gmu)]
    .map(chunk => Number(chunk[1]));
  const taskToChunk = new Map();
  let currentChunk = null;
  for (const line of source.split('\n')) {
    const chunk = /^## Chunk \d+ \(#(\d+)\):/u.exec(line);
    if (chunk) currentChunk = Number(chunk[1]);
    const task = /^### Task \d+:[^\n]*\(#(\d+)\)$/u.exec(line);
    if (task && currentChunk !== null) taskToChunk.set(Number(task[1]), currentChunk);
  }
  return { path: planPath, sha256: actual, taskOrder, chunkOrder, taskToChunk };
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
    owner_root: state.owner_root ?? null,
    owner_path: state.owner_path ?? null,
  });
}

function statesEqual(left, right) {
  return stateIdentity(left) === stateIdentity(right);
}

function parseWorktreeChanges(root) {
  const output = runGit(
    root,
    ['status', '--porcelain=v1', '-z', '--untracked-files=all', '--ignored=no'],
    { env: { GIT_OPTIONAL_LOCKS: '0' } },
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
      throw new SnapshotError(`rejected before read: unrelated checkout change ${changed}`);
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

function validCapturedState(state, evidenceRoot) {
  if (
    !isObject(state)
    || !['git', 'instruction'].includes(state.domain)
    || typeof state.exists !== 'boolean'
  ) {
    return false;
  }
  try {
    if (normalizeLogicalPath(state.path, evidenceRoot) !== state.path) return false;
  } catch {
    return false;
  }
  if (state.domain === 'instruction' && (
    !APPROVED_INSTRUCTION_PATHS.has(state.path)
    || typeof state.owner_root !== 'string'
    || state.owner_path !== state.path
  )) return false;
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
      || !validCapturedState(operation.before, context.evidenceRoot)
      || !validCapturedState(operation.after, context.evidenceRoot)
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
  const currentIndex = context.plan.taskOrder.indexOf(context.taskIssue);
  const expected = currentIndex < 0 ? [] : context.plan.taskOrder.slice(0, currentIndex);
  if (stableJson(taskIssues) !== stableJson(expected)) {
    throw new SnapshotError('prior task records are out of order');
  }
  if (records.length > 0) {
    materializeGitStates({
      root: context.root,
      startingCommit: context.startingCommit,
      operations: records.flatMap(record => record.operations)
        .filter(operation => operation.after.domain === 'git'),
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
    approved_paths: [...options.paths, ...options.instructionPaths],
    path_states: states.filter(state => state.domain === 'git'),
    instruction_path_states: states.filter(state => state.domain === 'instruction'),
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
  if (plan.taskToChunk.get(taskIssue) !== chunkIssue) {
    throw new SnapshotError('task and chunk do not match the approved plan');
  }
  const paths = validateUniquePaths(options.paths, evidenceRoot);
  const instructionPaths = options.instructionPaths ?? [];
  if (!Array.isArray(instructionPaths)) throw new ArgumentError('instruction paths must be an array');
  const normalizedInstructions = instructionPaths.map(path => normalizeLogicalPath(path, evidenceRoot));
  if (
    new Set(normalizedInstructions).size !== normalizedInstructions.length
    || normalizedInstructions.some(path => !APPROVED_INSTRUCTION_PATHS.has(path))
  ) throw new SnapshotError('rejected before read: unapproved instruction path');
  const context = {
    root,
    evidenceRoot,
    plan,
    startingCommit,
    chunkIssue,
    taskIssue,
    paths,
    instructionPaths: normalizedInstructions,
  };
  const priorTaskRecords = loadPriorTaskRecords(context, options.priorTaskRecords ?? []);
  return { ...context, priorTaskRecords };
}

function stateAfterPriorTasks(context, logicalPath) {
  let state = context.instructionPaths.includes(logicalPath)
    ? captureInstructionPathState(context, logicalPath, true)
    : gitTreeState(context.root, context.evidenceRoot, context.startingCommit, logicalPath);
  for (const record of context.priorTaskRecords) {
    for (const operation of record.operations) {
      if (operation.after.path === logicalPath) state = operation.after;
    }
  }
  return state;
}

function primaryCheckoutRoot(root) {
  const common = runGit(root, ['rev-parse', '--git-common-dir']).trim();
  const directory = isAbsolute(common) ? common : resolve(root, common);
  return realpathSync(dirname(directory));
}

function captureInstructionPathState(context, logicalPath, baseline = false) {
  if (!APPROVED_INSTRUCTION_PATHS.has(logicalPath)) {
    throw new SnapshotError(`rejected before read: ${logicalPath}`);
  }
  const ownerRoot = primaryCheckoutRoot(context.root);
  const target = resolve(ownerRoot, logicalPath);
  if (!inside(ownerRoot, target)) throw new SnapshotError(`rejected before read: ${logicalPath}`);
  const metadata = pathMetadata(target);
  const common = {
    path: logicalPath,
    domain: 'instruction',
    owner_root: ownerRoot,
    owner_path: logicalPath,
  };
  if (metadata === null) return { ...absentState(logicalPath), ...common };
  if (metadata.isSymbolicLink() || !metadata.isFile() || !inside(ownerRoot, realpathSync(target))) {
    throw new SnapshotError(`rejected before read: ${logicalPath}`);
  }
  let bytes;
  try { bytes = readFileSync(target); } catch {
    throw new SnapshotError(`unreadable path ${logicalPath}`);
  }
  return {
    ...presentState(
      context.root,
      context.evidenceRoot,
      logicalPath,
      (metadata.mode & 0o111) === 0 ? '100644' : '100755',
      bytes,
      'instruction',
    ),
    ...common,
    baseline,
  };
}

function allowedCapturePaths(context) {
  return [...new Set([
    ...context.paths,
    ...context.priorTaskRecords.flatMap(record => (
      record.operations.map(operation => operation.after.path)
    )),
  ])];
}

export function captureBaseline(options) {
  const context = captureContext(options);
  assertWorktreeChangesAllowed(
    context.root, context.evidenceRoot, allowedCapturePaths(context),
  );
  const states = [...context.paths, ...context.instructionPaths].map(logicalPath => {
    const before = stateAfterPriorTasks(context, logicalPath);
    const current = context.instructionPaths.includes(logicalPath)
      ? captureInstructionPathState(context, logicalPath)
      : capturePathState({
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
  assertWorktreeChangesAllowed(
    context.root, context.evidenceRoot, allowedCapturePaths(context),
  );
  const baseline = readRecord(
    context.root,
    context.evidenceRoot,
    options.baselinePath,
    'task-baseline',
  );
  assertRecordIdentity(baseline, context);
  if (
    stableJson(baseline.approved_paths)
    !== stableJson([...context.paths, ...context.instructionPaths])
  ) {
    throw new SnapshotError('baseline approved paths changed');
  }
  if (
    stableJson(baseline.prior_task_digests)
    !== stableJson(context.priorTaskRecords.map(record => record.digest_sha256))
  ) {
    throw new SnapshotError('baseline prior task records changed');
  }
  const operations = [];
  for (const before of [...baseline.path_states, ...baseline.instruction_path_states]) {
    let after;
    try {
      after = before.domain === 'instruction'
        ? captureInstructionPathState(context, before.path)
        : capturePathState({
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
  let bytes;
  try { bytes = readFileSync(target); } catch {
    throw new SnapshotError(`candidate is unreadable: ${logicalPath}`);
  }
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

function materializeGitStates({ root, startingCommit, startingTree = null, operations, evidenceRoot }) {
  const touched = [...new Set(operations.map(operation => operation.after.path))];
  const indexBefore = activeIndexIdentity(root);
  const worktreeBefore = touched.map(path => rawWorkingIdentity(root, path));
  const directory = mkdtempSync(resolve(tmpdir(), 'ferry-chunk-index-'));
  const indexPath = resolve(directory, 'index');
  try {
    const initialTree = startingTree
      ?? runGit(root, ['rev-parse', `${startingCommit}^{tree}`]).trim();
    runGit(root, ['read-tree', initialTree], { env: { GIT_INDEX_FILE: indexPath } });
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
    return { startingTree: initialTree, endingTree };
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
  const priorSnapshotPaths = options.priorSnapshotPaths ?? [];
  if (!Array.isArray(priorSnapshotPaths)) {
    throw new ArgumentError('prior snapshots must be an array');
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
  const approvedTaskOrder = plan.taskOrder;
  if (
    !plan.chunkOrder.includes(chunkIssue)
    || taskIssues.some(taskIssue => plan.taskToChunk.get(taskIssue) !== chunkIssue)
  ) throw new SnapshotError('task records do not belong to the approved chunk');
  const firstIndex = approvedTaskOrder.indexOf(taskIssues[0]);
  if (
    firstIndex < 0
    || stableJson(taskIssues)
      !== stableJson(approvedTaskOrder.slice(firstIndex, firstIndex + taskIssues.length))
  ) {
    throw new SnapshotError('task records do not match approved plan order');
  }
  let prefix = null;
  if (priorSnapshotPaths.length > 0) {
    try {
      prefix = reconstructChain({ root, snapshotPaths: priorSnapshotPaths, evidenceRoot });
    } catch (error) {
      throw new SnapshotError(`chunk ${chunkIssue}: ${error.message}`);
    }
  }
  const operations = taskRecords.flatMap(record => record.operations);
  const gitOperations = operations.filter(operation => operation.after.domain === 'git');
  let startingTree;
  let endingTree;
  try {
    ({ startingTree, endingTree } = materializeGitStates({
      root,
      startingCommit,
      startingTree: prefix?.finalTree ?? null,
      operations: gitOperations,
      evidenceRoot,
    }));
  } catch (error) {
    throw new SnapshotError(`chunk ${chunkIssue}: ${error.message}`);
  }
  const finalStates = new Map(
    (prefix?.instructionStates ?? []).map(state => [state.path, state]),
  );
  for (const operation of operations) finalStates.set(operation.after.path, operation.after);
  for (const state of finalStates.values()) {
    const current = state.domain === 'instruction'
      ? captureInstructionPathState({ root, evidenceRoot }, state.path)
      : capturePathState({
        root,
        logicalPath: state.path,
        domain: 'git',
        evidenceRoot,
      }).state;
    if (!statesEqual(current, state)) {
      throw new SnapshotError(
        `chunk ${chunkIssue}: candidate changed after task capture: ${state.path}`,
      );
    }
  }
  const record = finishRecord({
    ...commonRecord({ plan, startingCommit, chunkIssue }),
    kind: 'chunk-snapshot',
    prior_snapshot_digests: prefix?.snapshotDigests ?? [],
    task_issues: taskIssues,
    task_patch_digests: taskRecords.map(record => record.digest_sha256),
    task_record_paths: options.taskRecords,
    approved_paths: [...finalStates.keys()],
    starting_git_tree: startingTree,
    ending_git_tree: endingTree,
    starting_instruction_states: prefix?.instructionStates ?? [...new Map(
      operations
        .filter(operation => operation.before.domain === 'instruction')
        .map(operation => [operation.before.path, operation.before]),
    ).values()],
    ending_instruction_states: [...finalStates.values()]
      .filter(state => state.domain === 'instruction'),
  });
  const path = options.outputPath
    ?? recordPath(evidenceRoot, 'chunks', chunkIssue, 'snapshot.json');
  publishRecord(root, evidenceRoot, path, record);
  return { path, record };
}

function readArtifact(root, evidenceRoot, localPath, purpose) {
  const file = resolveEvidencePath(root, evidenceRoot, localPath, purpose);
  const bytes = readFileSync(file);
  return { path: localPath, sha256: sha256(bytes), bytes };
}

function artifactIdentity(artifact) {
  return { path: artifact.path, sha256: artifact.sha256 };
}

export function reconstructChain({ root: requestedRoot, snapshotPaths, evidenceRoot: requestedEvidence }) {
  const root = canonicalRoot(requestedRoot);
  const evidenceRoot = normalizeEvidenceRoot(requestedEvidence ?? DEFAULT_EVIDENCE_ROOT);
  if (!Array.isArray(snapshotPaths) || snapshotPaths.length === 0) {
    throw new SnapshotError('chunk snapshot chain is empty');
  }
  const indexBefore = activeIndexIdentity(root);
  const worktreeBefore = runGit(
    root,
    ['status', '--porcelain=v1', '-z', '--untracked-files=all'],
    { env: { GIT_OPTIONAL_LOCKS: '0' } },
  );
  const snapshots = snapshotPaths.map(path => readRecord(
    root, evidenceRoot, path, 'chunk-snapshot',
  ));
  const taskTrees = [];
  const taskIssues = [];
  let instructionStates = [];
  const gitStates = new Map();
  let finalTree = null;
  try {
    for (let index = 0; index < snapshots.length; index += 1) {
      const snapshot = snapshots[index];
      const expectedPrior = snapshots.slice(0, index).map(record => record.digest_sha256);
      if (stableJson(snapshot.prior_snapshot_digests) !== stableJson(expectedPrior)) {
        throw new SnapshotError(`chunk ${snapshot.chunk_issue}: prior snapshot order changed`);
      }
      const expectedStart = finalTree
        ?? runGit(root, ['rev-parse', `${snapshot.starting_commit}^{tree}`]).trim();
      if (snapshot.starting_git_tree !== expectedStart) {
        throw new SnapshotError(`chunk ${snapshot.chunk_issue}: starting tree identity changed`);
      }
      if (
        index > 0
        && stableJson(snapshot.starting_instruction_states) !== stableJson(instructionStates)
      ) throw new SnapshotError(`chunk ${snapshot.chunk_issue}: instruction start changed`);
      if (
        !Array.isArray(snapshot.task_record_paths)
        || snapshot.task_record_paths.length !== snapshot.task_issues.length
      ) {
        throw new SnapshotError(`chunk ${snapshot.chunk_issue}: task record paths are missing`);
      }
      let taskStart = expectedStart;
      for (let taskIndex = 0; taskIndex < snapshot.task_record_paths.length; taskIndex += 1) {
        const task = readRecord(
          root, evidenceRoot, snapshot.task_record_paths[taskIndex], 'task-patch',
        );
        if (
          task.task_issue !== snapshot.task_issues[taskIndex]
          || task.digest_sha256 !== snapshot.task_patch_digests[taskIndex]
          || task.starting_commit !== snapshot.starting_commit
        ) {
          throw new SnapshotError(`chunk ${snapshot.chunk_issue}: task identity changed`);
        }
        const built = materializeGitStates({
          root,
          startingCommit: snapshot.starting_commit,
          startingTree: taskStart,
          operations: task.operations.filter(operation => operation.after.domain === 'git'),
          evidenceRoot,
        });
        taskStart = built.endingTree;
        for (const operation of task.operations) {
          if (operation.after.domain === 'git') {
            gitStates.set(operation.after.path, operation.after);
          }
        }
        taskTrees.push(taskStart);
        taskIssues.push(task.task_issue);
      }
      if (taskStart !== snapshot.ending_git_tree) {
        throw new SnapshotError(`chunk ${snapshot.chunk_issue}: ending tree identity changed`);
      }
      finalTree = taskStart;
      instructionStates = snapshot.ending_instruction_states ?? [];
    }
    return {
      finalTree,
      taskTrees,
      taskIssues,
      snapshotDigests: snapshots.map(record => record.digest_sha256),
      instructionStates,
      gitStates: [...gitStates.values()],
      activeIndexBefore: indexBefore,
      activeIndexAfter: activeIndexIdentity(root),
      worktreeBefore,
      worktreeAfter: runGit(
        root,
        ['status', '--porcelain=v1', '-z', '--untracked-files=all'],
        { env: { GIT_OPTIONAL_LOCKS: '0' } },
      ),
    };
  } finally {
    if (
      activeIndexIdentity(root) !== indexBefore
      || runGit(
        root,
        ['status', '--porcelain=v1', '-z', '--untracked-files=all'],
        { env: { GIT_OPTIONAL_LOCKS: '0' } },
      ) !== worktreeBefore
    ) {
      throw new SnapshotError('chain reconstruction changed the active checkout');
    }
  }
}

function completeSnapshotForBinding(root, evidenceRoot, options) {
  const snapshot = readRecord(root, evidenceRoot, options.snapshotPath, 'chunk-snapshot');
  const plan = verifiedPlan(
    root,
    options.planPath ?? snapshot.plan_path,
    options.approvedPlanDigestPath ?? `${evidenceRoot}/approved-plan.sha256`,
    evidenceRoot,
  );
  if (
    snapshot.plan_path !== plan.path
    || snapshot.approved_plan_sha256 !== plan.sha256
    || !plan.chunkOrder.includes(snapshot.chunk_issue)
  ) {
    throw new SnapshotError('snapshot plan identity changed');
  }
  const expectedTasks = plan.taskOrder.filter(
    taskIssue => plan.taskToChunk.get(taskIssue) === snapshot.chunk_issue,
  );
  if (
    !Array.isArray(snapshot.task_issues)
    || !Array.isArray(snapshot.task_record_paths)
    || !Array.isArray(snapshot.task_patch_digests)
    || stableJson(snapshot.task_issues) !== stableJson(expectedTasks)
    || snapshot.task_record_paths.length !== expectedTasks.length
    || snapshot.task_patch_digests.length !== expectedTasks.length
  ) {
    throw new SnapshotError('snapshot does not contain the complete approved chunk task list');
  }
  for (let index = 0; index < expectedTasks.length; index += 1) {
    const task = readRecord(
      root,
      evidenceRoot,
      snapshot.task_record_paths[index],
      'task-patch',
    );
    if (
      task.task_issue !== expectedTasks[index]
      || task.chunk_issue !== snapshot.chunk_issue
      || task.plan_path !== plan.path
      || task.approved_plan_sha256 !== plan.sha256
      || task.digest_sha256 !== snapshot.task_patch_digests[index]
    ) {
      throw new SnapshotError('snapshot does not contain the complete approved chunk task list');
    }
  }
  return snapshot;
}

export function bindReview(options) {
  const root = canonicalRoot(options.root);
  const evidenceRoot = normalizeEvidenceRoot(options.evidenceRoot ?? DEFAULT_EVIDENCE_ROOT);
  const snapshot = completeSnapshotForBinding(root, evidenceRoot, options);
  const ensemble = readArtifact(
    root, evidenceRoot, options.ensembleArtifactPath, 'ensemble artifact',
  );
  const findings = (options.findingArtifactPaths ?? []).map(path => (
    readArtifact(root, evidenceRoot, path, 'finding artifact')
  ));
  const verdicts = (options.verdictArtifactPaths ?? []).map(path => (
    readArtifact(root, evidenceRoot, path, 'verdict artifact')
  ));
  const record = finishRecord({
    schema: SCHEMA,
    kind: 'review-binding',
    chunk_issue: snapshot.chunk_issue,
    snapshot_digest: snapshot.digest_sha256,
    ensemble_artifact: artifactIdentity(ensemble),
    finding_artifacts: findings.map(artifactIdentity),
    verdict_artifacts: verdicts.map(artifactIdentity),
  });
  const path = options.outputPath
    ?? recordPath(evidenceRoot, 'chunks', snapshot.chunk_issue, 'review-binding.json');
  publishRecord(root, evidenceRoot, path, record);
  return { path, record };
}

export function bindMarker(options) {
  const root = canonicalRoot(options.root);
  const evidenceRoot = normalizeEvidenceRoot(options.evidenceRoot ?? DEFAULT_EVIDENCE_ROOT);
  const snapshot = completeSnapshotForBinding(root, evidenceRoot, options);
  const review = readRecord(root, evidenceRoot, options.reviewBindingPath, 'review-binding');
  const commentArtifact = readArtifact(
    root, evidenceRoot, options.postedCommentReadbackPath, 'posted comment readback',
  );
  let comment;
  try { comment = JSON.parse(commentArtifact.bytes.toString('utf8')); } catch {
    throw new SnapshotError('posted comment readback is not valid JSON');
  }
  const marker = new RegExp(
    `<!-- review:chunk:[0-9]+ snapshot:${snapshot.digest_sha256} review:${review.digest_sha256} findings:[0-9]+ critical:[0-9]+ confirmed:[0-9]+ -->`,
    'u',
  );
  if (
    review.snapshot_digest !== snapshot.digest_sha256
    || !Number.isSafeInteger(comment?.id)
    || typeof comment?.html_url !== 'string'
    || !comment.html_url.includes(`/issues/${snapshot.chunk_issue}#`)
    || typeof comment?.body !== 'string'
    || !marker.test(comment.body)
    || typeof comment?.created_at !== 'string'
  ) {
    throw new SnapshotError('posted marker does not match the reviewed chunk');
  }
  const record = finishRecord({
    schema: SCHEMA,
    kind: 'marker-binding',
    chunk_issue: snapshot.chunk_issue,
    snapshot_digest: snapshot.digest_sha256,
    review_binding_digest: review.digest_sha256,
    comment: {
      id: comment.id,
      html_url: comment.html_url,
      body: comment.body,
      created_at: comment.created_at,
      readback_sha256: commentArtifact.sha256,
    },
  });
  const path = options.outputPath
    ?? recordPath(evidenceRoot, 'chunks', snapshot.chunk_issue, 'marker-binding.json');
  publishRecord(root, evidenceRoot, path, record);
  return { path, record };
}

const BACKUP_DESTINATIONS = new Map([
  ['AGENTS.md', ['projects/Discord-stoat-ferry/AGENTS.md']],
  ['.claude/skills/df-chunk-review/SKILL.md', [
    'projects/Discord-stoat-ferry/claude-dir/skills/df-chunk-review/SKILL.md',
    'skills-library/discord-stoat-ferry/df-chunk-review/SKILL.md',
  ]],
  ['.claude/skills/df-ship/SKILL.md', [
    'projects/Discord-stoat-ferry/claude-dir/skills/df-ship/SKILL.md',
    'skills-library/discord-stoat-ferry/df-ship/SKILL.md',
  ]],
  ['docs/architecture/adr/040-review-chunk-snapshots-before-task-commits.md', [
    'projects/Discord-stoat-ferry/adr/040-review-chunk-snapshots-before-task-commits.md',
  ]],
  ['docs/architecture/adr/README.md', ['projects/Discord-stoat-ferry/adr/README.md']],
]);

function assertBackupChangedPaths(instructions, changedPaths) {
  const allowed = new Set(instructions.flatMap(
    state => BACKUP_DESTINATIONS.get(state.path) ?? [],
  ));
  if (changedPaths.length === 0 || changedPaths.some(path => !allowed.has(path))) {
    throw new SnapshotError('backup commit changed paths outside the Ferry scope');
  }
}

function verifyBackupInstructionStates(root, evidenceRoot, backupRoot, commit, instructions) {
  for (const state of instructions) {
    const expected = checkedBlob(root, evidenceRoot, state);
    const destinations = BACKUP_DESTINATIONS.get(state.path) ?? [];
    if (destinations.length === 0) {
      throw new SnapshotError(`backup destination mapping is missing: ${state.path}`);
    }
    for (const destination of destinations) {
      const entry = runGit(
        backupRoot, ['ls-tree', '-z', commit, '--', destination], { encoding: null },
      );
      if (entry.length === 0) {
        throw new SnapshotError(`backup destination is missing: ${destination}`);
      }
      const match = /^(100644|100755) blob ([a-f0-9]{40,64})\t(.+)\0$/u.exec(
        entry.toString('utf8'),
      );
      if (!match || match[3] !== destination) {
        throw new SnapshotError(`backup destination is invalid: ${destination}`);
      }
      if (match[1] !== state.mode) {
        throw new SnapshotError(`backup mode changed: ${destination}`);
      }
      const actual = runGit(backupRoot, ['cat-file', 'blob', match[2]], { encoding: null });
      if (!actual.equals(expected)) {
        throw new SnapshotError(`backup bytes changed: ${destination}`);
      }
    }
  }
}

export function bindBackup(options) {
  const root = canonicalRoot(options.root);
  const evidenceRoot = normalizeEvidenceRoot(options.evidenceRoot ?? DEFAULT_EVIDENCE_ROOT);
  const task = readRecord(root, evidenceRoot, options.taskRecordPath, 'task-patch');
  const instructions = task.operations
    .filter(operation => operation.after.domain === 'instruction')
    .map(operation => operation.after);
  if (instructions.length === 0) throw new SnapshotError('backup task has no instruction states');
  const backupRoot = canonicalRoot(options.backupRepoPath);
  if (basename(backupRoot) !== 'claude-setup') {
    throw new SnapshotError('backup repository is not the approved claude-setup checkout');
  }
  const commit = runGit(backupRoot, ['rev-parse', `${options.backupCommitOid}^{commit}`]).trim();
  const parents = runGit(backupRoot, ['rev-list', '--parents', '-n', '1', commit]).trim().split(' ');
  if (parents.length !== 2) throw new SnapshotError('backup commit must have one parent');
  const changedPaths = runGit(
    backupRoot, ['diff-tree', '--no-commit-id', '--name-only', '-r', parents[1], commit],
  ).trim().split('\n').filter(Boolean);
  assertBackupChangedPaths(instructions, changedPaths);
  verifyBackupInstructionStates(root, evidenceRoot, backupRoot, commit, instructions);
  const snapshotLog = readArtifact(
    root, evidenceRoot, options.snapshotLogPath, 'backup snapshot log',
  );
  const record = finishRecord({
    schema: SCHEMA,
    kind: 'backup-binding',
    task_issue: task.task_issue,
    task_patch_digest: task.digest_sha256,
    snapshot_log: artifactIdentity(snapshotLog),
    backup_repo: backupRoot,
    backup_commit: commit,
    backup_parent: parents[1],
    changed_paths: changedPaths,
    instruction_states: instructions,
  });
  const path = options.outputPath
    ?? recordPath(evidenceRoot, 'tasks', task.task_issue, 'backup-binding.json');
  publishRecord(root, evidenceRoot, path, record);
  return { path, record };
}

function verifyArtifactReferences(root, evidenceRoot, references) {
  for (const reference of references) {
    const artifact = readArtifact(root, evidenceRoot, reference.path, 'bound artifact');
    if (artifact.sha256 !== reference.sha256) throw new SnapshotError('bound artifact changed');
  }
}

function readBoundJsonArtifact(root, evidenceRoot, reference, purpose) {
  if (
    !isObject(reference)
    || typeof reference.path !== 'string'
    || !/^[a-f0-9]{64}$/u.test(reference.sha256 ?? '')
  ) throw new SnapshotError(`${purpose} identity is invalid`);
  const artifact = readArtifact(root, evidenceRoot, reference.path, purpose);
  if (artifact.sha256 !== reference.sha256) {
    throw new SnapshotError(`${purpose} bytes changed`);
  }
  try {
    return JSON.parse(artifact.bytes.toString('utf8'));
  } catch {
    throw new SnapshotError(`${purpose} is not valid JSON`);
  }
}

function currentRuleMatches(root, currentRule) {
  if (
    !isObject(currentRule)
    || !CURRENT_RULE_PATHS.has(currentRule.path)
    || typeof currentRule.quote !== 'string'
    || currentRule.quote.length < 16
  ) return false;
  const ownerRoot = primaryCheckoutRoot(root);
  const file = resolve(ownerRoot, currentRule.path);
  const metadata = pathMetadata(file);
  if (
    metadata === null
    || metadata.isSymbolicLink()
    || !metadata.isFile()
    || !inside(ownerRoot, realpathSync(file))
  ) return false;
  return readFileSync(file, 'utf8').includes(currentRule.quote);
}

function findingAuthorityApplies({ root, planSource, snapshot, finding, gate }) {
  const commandAuthorization = authorizeVerificationCommand(
    finding?.verification?.command,
    { root },
  );
  const authority = gate.authority;
  const reviewedPath = typeof finding?.file === 'string'
    ? finding.file.replace(/^(?:a|b)\//u, '')
    : null;
  const allowedBasis = new Set([
    'acceptance criteria',
    'required safety',
    'data integrity',
    'contract correctness',
  ]);
  const approvedSource = (
    typeof authority?.approved_outcome === 'string'
    && authority.approved_outcome.length > 0
    && planSource.includes(authority.approved_outcome)
  ) || currentRuleMatches(root, authority?.current_rule);
  return {
    commandAuthorization,
    authorized: commandAuthorization.authorized
      && isObject(authority)
      && approvedSource
      && authority.reviewed_path === reviewedPath
      && snapshot.approved_paths.includes(reviewedPath)
      && allowedBasis.has(authority.blocking_basis),
  };
}

function resolutionIsValid({
  root,
  evidenceRoot,
  snapshot,
  finding,
  commandAuthorization,
  resolution,
}) {
  if (!isObject(resolution) || resolution.snapshot_digest !== snapshot.digest_sha256) {
    return false;
  }
  const fixedResult = readBoundJsonArtifact(
    root,
    evidenceRoot,
    resolution.result_artifact,
    'fixed finding result',
  );
  const freshChecks = readBoundJsonArtifact(
    root,
    evidenceRoot,
    resolution.fresh_checks_artifact,
    'fresh checks artifact',
  );
  const scopedReview = readBoundJsonArtifact(
    root,
    evidenceRoot,
    resolution.scoped_review_artifact,
    'scoped review artifact',
  );
  if (
    !isObject(freshChecks)
    || freshChecks.status !== 'passed'
    || freshChecks.snapshot_digest !== snapshot.digest_sha256
    || !Array.isArray(freshChecks.commands)
    || freshChecks.commands.length === 0
    || freshChecks.commands.some(command => (
      !isObject(command)
      || typeof command.command !== 'string'
      || command.command.length === 0
      || command.exit_code !== 0
    ))
  ) throw new SnapshotError('fresh checks artifact is not a passing snapshot-bound result');
  const expectedProviders = new Map([
    ['mistral-vibe', 'zai-glm-5-2'],
    ['qwen', 'qwen3.8-max'],
  ]);
  const advisoryStatuses = new Set(['valid', 'failed', 'timed_out', 'interrupted']);
  if (
    !isObject(scopedReview)
    || scopedReview.mode !== 'scoped-fix-review'
    || scopedReview.snapshot_digest !== snapshot.digest_sha256
    || !Array.isArray(scopedReview.slots)
    || scopedReview.slots.length !== expectedProviders.size
    || scopedReview.slots.some(slot => (
      !isObject(slot)
      || expectedProviders.get(slot.slot) !== slot.model
      || !advisoryStatuses.has(slot.status)
    ))
    || new Set(scopedReview.slots.map(slot => slot.slot)).size !== expectedProviders.size
  ) throw new SnapshotError('scoped review artifact does not preserve the fixed provider contract');
  const computed = commandAuthorization.authorized
    ? classifyVerification(finding, fixedResult, commandAuthorization)
    : 'INCONCLUSIVE';
  if (resolution.verdict !== computed) {
    throw new SnapshotError('fixed finding verdict does not match installed classifier');
  }
  return computed === 'REFUTED';
}

function validateFindingGates({ root, evidenceRoot, plan, snapshot, review }) {
  if (review.finding_artifacts.length !== review.verdict_artifacts.length) {
    throw new SnapshotError('finding and verdict artifact counts differ');
  }
  const planSource = readFileSync(resolve(root, plan.path), 'utf8');
  for (let index = 0; index < review.finding_artifacts.length; index += 1) {
    const findingReference = review.finding_artifacts[index];
    const finding = readBoundJsonArtifact(
      root,
      evidenceRoot,
      findingReference,
      'finding artifact',
    );
    const gate = readBoundJsonArtifact(
      root,
      evidenceRoot,
      review.verdict_artifacts[index],
      'finding verdict artifact',
    );
    if (
      !isObject(gate)
      || stableJson(gate.finding_artifact) !== stableJson(findingReference)
    ) throw new SnapshotError('finding verdict names another finding');
    const result = readBoundJsonArtifact(
      root,
      evidenceRoot,
      gate.result_artifact,
      'finding result artifact',
    );
    const authority = findingAuthorityApplies({ root, planSource, snapshot, finding, gate });
    const computed = authority.commandAuthorization.authorized
      ? classifyVerification(finding, result, authority.commandAuthorization)
      : 'INCONCLUSIVE';
    if (gate.verdict !== computed) {
      throw new SnapshotError('finding verdict does not match installed classifier');
    }
    let resolved = false;
    if (gate.resolution !== undefined) {
      resolved = resolutionIsValid({
        root,
        evidenceRoot,
        snapshot,
        finding,
        commandAuthorization: authority.commandAuthorization,
        resolution: gate.resolution,
      });
    }
    if (
      finding.severity === 'critical'
      && computed === 'CONFIRMED'
      && authority.authorized
      && !resolved
    ) throw new SnapshotError('authorized confirmed critical finding is unresolved');
  }
}

function revalidateBackupBinding(root, evidenceRoot, backup) {
  const backupRoot = canonicalRoot(backup.backup_repo);
  if (basename(backupRoot) !== 'claude-setup') {
    throw new SnapshotError('backup repository identity changed');
  }
  const commit = runGit(backupRoot, ['rev-parse', `${backup.backup_commit}^{commit}`]).trim();
  const ancestry = runGit(backupRoot, ['rev-list', '--parents', '-n', '1', commit])
    .trim().split(' ');
  if (ancestry.length !== 2 || ancestry[1] !== backup.backup_parent) {
    throw new SnapshotError('backup commit ancestry changed');
  }
  const changedPaths = runGit(
    backupRoot, ['diff-tree', '--no-commit-id', '--name-only', '-r', ancestry[1], commit],
  ).trim().split('\n').filter(Boolean);
  assertBackupChangedPaths(backup.instruction_states, changedPaths);
  if (stableJson(changedPaths) !== stableJson(backup.changed_paths)) {
    throw new SnapshotError('backup changed-path evidence changed');
  }
  verifyArtifactReferences(root, evidenceRoot, [backup.snapshot_log]);
  verifyBackupInstructionStates(
    root, evidenceRoot, backupRoot, commit, backup.instruction_states,
  );
}

function validateBackupTaskBinding(root, evidenceRoot, snapshots, backup) {
  const matchingTasks = [];
  for (const snapshot of snapshots) {
    for (let index = 0; index < snapshot.task_record_paths.length; index += 1) {
      const task = readRecord(
        root, evidenceRoot, snapshot.task_record_paths[index], 'task-patch',
      );
      if (task.digest_sha256 !== snapshot.task_patch_digests[index]) {
        throw new SnapshotError('snapshot task digest changed');
      }
      if (task.task_issue === backup.task_issue) matchingTasks.push(task);
    }
  }
  if (matchingTasks.length !== 1) {
    throw new SnapshotError('instruction backup task identity changed');
  }
  const [task] = matchingTasks;
  if (task.digest_sha256 !== backup.task_patch_digest) {
    throw new SnapshotError('instruction backup task digest changed');
  }
  const instructionStates = task.operations
    .filter(operation => operation.after.domain === 'instruction')
    .map(operation => operation.after);
  if (stableJson(instructionStates) !== stableJson(backup.instruction_states)) {
    throw new SnapshotError('instruction backup task instruction states changed');
  }
}

export function validateChain(options) {
  const root = canonicalRoot(options.root);
  const evidenceRoot = normalizeEvidenceRoot(options.evidenceRoot ?? DEFAULT_EVIDENCE_ROOT);
  const plan = verifiedPlan(root, options.planPath, options.approvedPlanDigestPath, evidenceRoot);
  const phase = options.phase;
  if (!['review', 'shipping', 'committed'].includes(phase)) {
    throw new ArgumentError('validation phase is invalid');
  }
  const reconstruction = reconstructChain({
    root, snapshotPaths: options.snapshotPaths, evidenceRoot,
  });
  const snapshots = options.snapshotPaths.map(path => readRecord(
    root, evidenceRoot, path, 'chunk-snapshot',
  ));
  const currentHead = runGit(root, ['rev-parse', 'HEAD']).trim();
  if (phase !== 'committed' && currentHead !== snapshots[0].starting_commit) {
    throw new SnapshotError('HEAD changed from the reviewed starting commit');
  }
  if (snapshots.some(snapshot => (
    snapshot.plan_path !== plan.path || snapshot.approved_plan_sha256 !== plan.sha256
  ))) throw new SnapshotError('chunk plan identity changed');
  const expectedChunks = plan.chunkOrder.slice(0, snapshots.length);
  if (stableJson(snapshots.map(snapshot => snapshot.chunk_issue)) !== stableJson(expectedChunks)) {
    throw new SnapshotError('chunk snapshots do not match the approved plan order');
  }
  assertWorktreeChangesAllowed(
    root,
    evidenceRoot,
    [
      ...reconstruction.gitStates.map(state => state.path),
      ...reconstruction.instructionStates.map(state => state.path),
    ],
  );
  for (const state of reconstruction.gitStates) {
    let current;
    try {
      current = capturePathState({
        root,
        logicalPath: state.path,
        domain: 'git',
        evidenceRoot,
      }).state;
    } catch (error) {
      throw new SnapshotError(`final candidate changed: ${state.path}: ${error.message}`);
    }
    if (!statesEqual(current, state)) {
      throw new SnapshotError(`final candidate changed: ${state.path}`);
    }
  }
  const singleChunkFull = plan.chunkOrder.length === 1 && snapshots.length === 1;
  const expectedReviewed = singleChunkFull
    ? 0
    : phase === 'review' ? snapshots.length - 1 : snapshots.length;
  const reviews = options.reviewBindingPaths ?? [];
  const markers = options.markerBindingPaths ?? [];
  const readbacks = options.parentCommentsReadbackPaths ?? [];
  if (
    reviews.length !== expectedReviewed
    || markers.length !== expectedReviewed
    || readbacks.length !== expectedReviewed
  ) throw new SnapshotError('chunk review evidence count changed');
  for (let index = 0; index < expectedReviewed; index += 1) {
    const review = readRecord(root, evidenceRoot, reviews[index], 'review-binding');
    const marker = readRecord(root, evidenceRoot, markers[index], 'marker-binding');
    if (
      review.snapshot_digest !== snapshots[index].digest_sha256
      || marker.snapshot_digest !== snapshots[index].digest_sha256
      || marker.review_binding_digest !== review.digest_sha256
    ) throw new SnapshotError('chunk review binding identity changed');
    verifyArtifactReferences(root, evidenceRoot, [
      review.ensemble_artifact,
      ...review.finding_artifacts,
      ...review.verdict_artifacts,
    ]);
    const comments = JSON.parse(readArtifact(
      root, evidenceRoot, readbacks[index], 'parent comments readback',
    ).bytes.toString('utf8'));
    if (!Array.isArray(comments) || !comments.some(comment => (
      comment.id === marker.comment.id && comment.body === marker.comment.body
    ))) throw new SnapshotError('fresh parent comment readback is missing the marker');
    validateFindingGates({ root, evidenceRoot, plan, snapshot: snapshots[index], review });
  }
  const expectedTasks = plan.taskOrder.slice(0, reconstruction.taskIssues.length);
  if (stableJson(reconstruction.taskIssues) !== stableJson(expectedTasks)) {
    throw new SnapshotError('task sequence does not match the approved plan');
  }
  if (
    phase !== 'review'
    && stableJson(reconstruction.taskIssues) !== stableJson(plan.taskOrder)
  ) {
    throw new SnapshotError('shipping requires the complete approved task sequence');
  }
  if (reconstruction.instructionStates.length > 0) {
    if (!options.backupEvidencePath) {
      throw new SnapshotError('scoped instruction backup evidence is required');
    }
    const backup = readRecord(
      root, evidenceRoot, options.backupEvidencePath, 'backup-binding',
    );
    validateBackupTaskBinding(root, evidenceRoot, snapshots, backup);
    revalidateBackupBinding(root, evidenceRoot, backup);
    if (stableJson(backup.instruction_states) !== stableJson(reconstruction.instructionStates)) {
      throw new SnapshotError('instruction backup identity changed');
    }
    for (const state of reconstruction.instructionStates) {
      const current = captureInstructionPathState({ root, evidenceRoot }, state.path);
      if (!statesEqual(current, state)) {
        throw new SnapshotError(`instruction candidate changed: ${state.path}`);
      }
    }
  }
  let closureEligible = [];
  if (phase === 'committed') {
    const commits = options.commitOids ?? [];
    if (commits.length !== reconstruction.taskTrees.length) {
      throw new SnapshotError('one commit per reviewed task is required');
    }
    if (currentHead !== commits.at(-1)) {
      throw new SnapshotError('HEAD does not name the final reviewed task commit');
    }
    commits.forEach((commit, index) => {
      const tree = runGit(root, ['rev-parse', `${commit}^{tree}`]).trim();
      if (tree !== reconstruction.taskTrees[index]) {
        throw new SnapshotError('task commit tree does not match reviewed content');
      }
      const ancestry = runGit(root, ['rev-list', '--parents', '-n', '1', commit])
        .trim().split(' ');
      const expectedParent = index === 0 ? snapshots[0].starting_commit : commits[index - 1];
      if (ancestry.length !== 2 || ancestry[1] !== expectedParent) {
        throw new SnapshotError('task commit parent does not match the approved sequence');
      }
    });
    closureEligible = reconstruction.taskIssues;
  }
  return {
    ok: true,
    phase,
    snapshotDigests: reconstruction.snapshotDigests,
    taskTrees: reconstruction.taskTrees,
    taskIssues: reconstruction.taskIssues,
    finalTree: reconstruction.finalTree,
    closureEligible,
    failures: [],
  };
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
    'Snapshot commands:',
    '  import-bootstrap  Import a verified clean pre-helper baseline',
    '  baseline          Capture a Git-derived task baseline',
    '  capture-task      Capture exact ordered task operations',
    '  capture-chunk     Capture ordered task patches and Git tree identities',
    '  bind-review       Bind installed-contract review artifacts',
    '  bind-marker       Bind a real posted-comment readback',
    '  bind-backup       Bind a scoped instruction backup commit',
    '  validate-chain    Validate review, shipping, or committed evidence',
  ].join('\n');
}

function parseArgs(argv) {
  if (argv.length === 0 || argv[0] === '--help' || argv[0] === '-h') return { help: true };
  const command = argv[0];
  const commands = new Set([
    'import-bootstrap', 'baseline', 'capture-task', 'capture-chunk',
    'bind-review', 'bind-marker', 'bind-backup', 'validate-chain',
  ]);
  if (!commands.has(command)) throw new ArgumentError(`unknown command: ${command}`);
  const repeatable = new Set([
    '--prior-task-record', '--prior-snapshot', '--task', '--task-record', '--snapshot',
    '--finding-artifact', '--verdict-artifact', '--review-binding', '--marker-binding',
    '--parent-comments-readback', '--commit',
  ]);
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
    'bind-review': new Set([
      '--root', '--plan', '--approved-plan-digest', '--snapshot', '--ensemble-artifact',
      '--finding-artifact', '--verdict-artifact', '--output',
    ]),
    'bind-marker': new Set([
      '--root', '--plan', '--approved-plan-digest', '--snapshot', '--review-binding',
      '--posted-comment-readback', '--output',
    ]),
    'bind-backup': new Set([
      '--root', '--plan', '--approved-plan-digest', '--task-record', '--snapshot-log',
      '--backup-repo', '--backup-commit', '--output',
    ]),
    'validate-chain': new Set([
      '--root', '--plan', '--approved-plan-digest', '--snapshot', '--phase',
      '--review-binding', '--marker-binding', '--parent-comments-readback', '--commit',
      '--backup-evidence',
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

function oneRepeated(values, flag) {
  const entries = values[flag] ?? [];
  if (entries.length !== 1) throw new ArgumentError(`exactly one ${flag} is required`);
  return entries[0];
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
  if (command === 'bind-review') {
    verifiedPlan(common.root, common.planPath, common.approvedPlanDigestPath, common.evidenceRoot);
    printResult(bindReview({
      ...common,
      snapshotPath: oneRepeated(values, '--snapshot'),
      ensembleArtifactPath: required(values, '--ensemble-artifact'),
      findingArtifactPaths: values['--finding-artifact'] ?? [],
      verdictArtifactPaths: values['--verdict-artifact'] ?? [],
      outputPath: values['--output'] ?? null,
    }));
    return;
  }
  if (command === 'bind-marker') {
    verifiedPlan(common.root, common.planPath, common.approvedPlanDigestPath, common.evidenceRoot);
    printResult(bindMarker({
      ...common,
      snapshotPath: oneRepeated(values, '--snapshot'),
      reviewBindingPath: oneRepeated(values, '--review-binding'),
      postedCommentReadbackPath: required(values, '--posted-comment-readback'),
      outputPath: values['--output'] ?? null,
    }));
    return;
  }
  if (command === 'bind-backup') {
    verifiedPlan(common.root, common.planPath, common.approvedPlanDigestPath, common.evidenceRoot);
    printResult(bindBackup({
      ...common,
      taskRecordPath: oneRepeated(values, '--task-record'),
      snapshotLogPath: required(values, '--snapshot-log'),
      backupRepoPath: required(values, '--backup-repo'),
      backupCommitOid: required(values, '--backup-commit'),
      outputPath: values['--output'] ?? null,
    }));
    return;
  }
  if (command === 'validate-chain') {
    process.stdout.write(stableJson(validateChain({
      ...common,
      snapshotPaths: values['--snapshot'] ?? [],
      phase: required(values, '--phase'),
      reviewBindingPaths: values['--review-binding'] ?? [],
      markerBindingPaths: values['--marker-binding'] ?? [],
      parentCommentsReadbackPaths: values['--parent-comments-readback'] ?? [],
      commitOids: values['--commit'] ?? [],
      backupEvidencePath: values['--backup-evidence'] ?? null,
    })));
    return;
  }
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
  const instructionPaths = values['--instruction-paths-file']
    ? readPathsFile(
      common.root, common.evidenceRoot, values['--instruction-paths-file'],
    )
    : [];
  const options = {
    ...common,
    chunkIssue,
    taskIssue,
    startingCommit,
    paths,
    instructionPaths,
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
