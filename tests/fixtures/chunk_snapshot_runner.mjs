#!/usr/bin/env node

import {
  chmodSync,
  mkdirSync,
  readFileSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from 'node:fs';
import { join, resolve } from 'node:path';
import { spawnSync } from 'node:child_process';
import {
  captureBaseline,
  captureChunk,
  captureTask,
  sha256,
} from '../../scripts/agent-compat/chunk-snapshot.mjs';

function git(root, ...args) {
  const result = spawnSync('git', args, { cwd: root, encoding: 'utf8' });
  if (result.status !== 0) throw new Error(result.stderr.trim());
  return result.stdout.trim();
}

function writeJson(value) {
  process.stdout.write(`${JSON.stringify(value)}\n`);
}

function optionsFor(root, paths) {
  const planPath = 'docs/plans/fixture-plan.md';
  const evidenceRoot = 'docs/plans/.review/chunk-snapshots';
  mkdirSync(join(root, evidenceRoot), { recursive: true });
  const approvedPlanDigestPath = `${evidenceRoot}/approved-plan.sha256`;
  const digest = sha256(readFileSync(join(root, planPath)));
  writeFileSync(join(root, approvedPlanDigestPath), `${digest}  ${planPath}\n`);
  return {
    root,
    planPath,
    approvedPlanDigestPath,
    chunkIssue: 1001,
    taskIssue: 1002,
    startingCommit: git(root, 'rev-parse', 'HEAD'),
    paths,
    instructionPaths: [],
    priorTaskRecords: [],
    priorSnapshotPaths: [],
    evidenceRoot,
  };
}

function unsafeEvidenceOptions(root, evidenceRoot) {
  return {
    root,
    planPath: 'docs/plans/fixture-plan.md',
    approvedPlanDigestPath: `${evidenceRoot}/approved-plan.sha256`,
    chunkIssue: 1001,
    taskIssue: 1002,
    startingCommit: git(root, 'rev-parse', 'HEAD'),
    paths: ['plain.txt'],
    instructionPaths: [],
    priorTaskRecords: [],
    priorSnapshotPaths: [],
    evidenceRoot,
  };
}

async function main([fixture, directory, variant]) {
  const root = resolve(directory, 'repo');
  mkdirSync(root, { recursive: true });
  git(root, 'init', '-q');
  git(root, 'config', 'user.email', 'fixture@example.com');
  git(root, 'config', 'user.name', 'Fixture');
  if (fixture === 'capture-content') {
    mkdirSync(join(root, 'docs/plans'), { recursive: true });
    writeFileSync(join(root, 'docs/plans/fixture-plan.md'), '# Fixture plan\n');
    writeFileSync(join(root, 'plain.txt'), 'before\n');
    writeFileSync(join(root, 'binary.bin'), Buffer.from([0, 255, 1, 254]));
    writeFileSync(join(root, 'run.sh'), '#!/bin/sh\nexit 0\n');
    chmodSync(join(root, 'run.sh'), 0o755);
    writeFileSync(join(root, 'deleted.txt'), 'delete me\n');
    git(
      root,
      'add',
      'docs/plans/fixture-plan.md',
      'plain.txt',
      'binary.bin',
      'run.sh',
      'deleted.txt',
    );
    git(root, 'commit', '-qm', 'fixture baseline');
    const options = optionsFor(
      root,
      ['plain.txt', 'binary.bin', 'run.sh', 'deleted.txt', 'new.txt'],
    );
    const baseline = await captureBaseline(options);
    writeFileSync(join(root, 'plain.txt'), 'after\n');
    writeFileSync(join(root, 'binary.bin'), Buffer.from([0, 1, 2, 3, 255]));
    writeFileSync(join(root, 'run.sh'), '#!/bin/sh\nexit 1\n');
    chmodSync(join(root, 'run.sh'), 0o755);
    writeFileSync(join(root, 'new.txt'), 'new\n');
    rmSync(join(root, 'deleted.txt'));
    const task = await captureTask({ ...options, baselinePath: baseline.path });
    const chunk = await captureChunk({ ...options, taskRecords: [task.path] });
    const operations = task.record.operations;
    const states = Object.fromEntries(
      operations.map(operation => [operation.after.path, operation]),
    );
    const savedBinary = readFileSync(
      join(root, options.evidenceRoot, states['binary.bin'].after.blob),
    );
    const blobMatches = state => !state.exists || (
      sha256(readFileSync(join(root, options.evidenceRoot, state.blob))) === state.sha256
    );
    const expectedStartingTree = git(root, 'rev-parse', 'HEAD^{tree}');
    git(
      root,
      'add',
      '-A',
      '--',
      'plain.txt',
      'binary.bin',
      'run.sh',
      'deleted.txt',
      'new.txt',
    );
    const expectedEndingTree = git(root, 'write-tree');
    writeJson({
      round_trip: operations.length === 5
        && chunk.record.task_patch_digests.length === 1
        && operations.every(operation => (
          blobMatches(operation.before) && blobMatches(operation.after)
        )),
      modes: [...new Set(
        operations
          .filter(operation => operation.after.exists)
          .map(operation => operation.after.mode),
      )].sort(),
      new_file: {
        before: states['new.txt'].before.exists,
        after: states['new.txt'].after.exists,
      },
      deleted_file: {
        before: states['deleted.txt'].before.exists,
        after: states['deleted.txt'].after.exists,
      },
      binary_sha256: sha256(Buffer.from([0, 1, 2, 3, 255])),
      saved_binary_sha256: sha256(savedBinary),
      starting_tree: chunk.record.starting_git_tree,
      expected_starting_tree: expectedStartingTree,
      ending_tree: chunk.record.ending_git_tree,
      expected_ending_tree: expectedEndingTree,
    });
    return;
  }
  if (fixture === 'capture-unreadable') {
    if (process.geteuid?.() === 0) {
      writeJson({ permission_guard_unsupported: true });
      return;
    }
    mkdirSync(join(root, 'docs/plans'), { recursive: true });
    writeFileSync(join(root, 'docs/plans/fixture-plan.md'), '# Fixture plan\n');
    writeFileSync(join(root, 'unreadable.bin'), Buffer.from([1, 2, 3]));
    git(root, 'add', 'docs/plans/fixture-plan.md', 'unreadable.bin');
    git(root, 'commit', '-qm', 'fixture baseline');
    const options = optionsFor(root, ['unreadable.bin']);
    const baseline = await captureBaseline(options);
    chmodSync(join(root, 'unreadable.bin'), 0o000);
    await captureTask({ ...options, baselinePath: baseline.path });
    throw new Error('capture unexpectedly accepted unreadable.bin');
  }
  if (fixture === 'capture-dangling-link') {
    mkdirSync(join(root, 'docs/plans'), { recursive: true });
    writeFileSync(join(root, 'docs/plans/fixture-plan.md'), '# Fixture plan\n');
    git(root, 'add', 'docs/plans/fixture-plan.md');
    git(root, 'commit', '-qm', 'fixture baseline');
    const options = optionsFor(root, ['dangling.txt']);
    const baseline = await captureBaseline(options);
    symlinkSync('missing-target.txt', join(root, 'dangling.txt'));
    await captureTask({ ...options, baselinePath: baseline.path });
    throw new Error('capture unexpectedly accepted dangling.txt');
  }
  if (fixture === 'reject-escaping-evidence-root') {
    mkdirSync(join(root, 'docs/plans'), { recursive: true });
    writeFileSync(join(root, 'docs/plans/fixture-plan.md'), '# Fixture plan\n');
    writeFileSync(join(root, 'plain.txt'), 'before\n');
    git(root, 'add', 'docs/plans/fixture-plan.md', 'plain.txt');
    git(root, 'commit', '-qm', 'fixture baseline');
    await captureBaseline(unsafeEvidenceOptions(root, '../outside-evidence'));
    throw new Error('capture unexpectedly accepted an escaping evidence root');
  }
  if (fixture === 'reject-dangling-evidence-directory') {
    mkdirSync(join(root, 'docs/plans'), { recursive: true });
    writeFileSync(join(root, 'docs/plans/fixture-plan.md'), '# Fixture plan\n');
    writeFileSync(join(root, 'plain.txt'), 'before\n');
    git(root, 'add', 'docs/plans/fixture-plan.md', 'plain.txt');
    git(root, 'commit', '-qm', 'fixture baseline');
    mkdirSync(join(root, 'docs/plans/.review'), { recursive: true });
    symlinkSync(
      '../../../../missing-evidence-target',
      join(root, 'docs/plans/.review/chunk-snapshots'),
    );
    await captureBaseline(
      unsafeEvidenceOptions(root, 'docs/plans/.review/chunk-snapshots'),
    );
    throw new Error('capture unexpectedly accepted a dangling evidence directory');
  }
  if (fixture === 'prior-task-baseline') {
    mkdirSync(join(root, 'docs/plans'), { recursive: true });
    writeFileSync(join(root, 'docs/plans/fixture-plan.md'), '# Fixture plan\n');
    writeFileSync(join(root, 'plain.txt'), 'before\n');
    git(root, 'add', 'docs/plans/fixture-plan.md', 'plain.txt');
    git(root, 'commit', '-qm', 'fixture baseline');
    const firstOptions = optionsFor(root, ['plain.txt']);
    const firstBaseline = await captureBaseline(firstOptions);
    writeFileSync(join(root, 'plain.txt'), 'task one\n');
    const firstTask = await captureTask({
      ...firstOptions,
      baselinePath: firstBaseline.path,
    });
    const secondOptions = {
      ...firstOptions,
      taskIssue: 1003,
      priorTaskRecords: [firstTask.path],
    };
    if (variant === 'missing') {
      secondOptions.priorTaskRecords = [
        `${firstOptions.evidenceRoot}/tasks/9999/patch.json`,
      ];
    } else if (variant === 'damaged') {
      writeFileSync(join(root, firstTask.path), '{}\n');
    } else if (variant === 'current-mismatch') {
      writeFileSync(join(root, 'plain.txt'), 'changed after task capture\n');
    }
    const secondBaseline = await captureBaseline(secondOptions);
    writeJson({
      prior_task_digests: secondBaseline.record.prior_task_digests,
      expected_prior_digest: firstTask.record.digest_sha256,
      baseline_sha256: secondBaseline.record.path_states[0].sha256,
      current_sha256: sha256(readFileSync(join(root, 'plain.txt'))),
    });
    return;
  }
  if (fixture === 'prior-task-reordered') {
    mkdirSync(join(root, 'docs/plans'), { recursive: true });
    writeFileSync(join(root, 'docs/plans/fixture-plan.md'), '# Fixture plan\n');
    writeFileSync(join(root, 'first.txt'), 'before first\n');
    writeFileSync(join(root, 'second.txt'), 'before second\n');
    git(root, 'add', 'docs/plans/fixture-plan.md', 'first.txt', 'second.txt');
    git(root, 'commit', '-qm', 'fixture baseline');
    const firstOptions = optionsFor(root, ['first.txt']);
    const firstBaseline = await captureBaseline(firstOptions);
    writeFileSync(join(root, 'first.txt'), 'after first\n');
    const firstTask = await captureTask({
      ...firstOptions,
      baselinePath: firstBaseline.path,
    });
    writeFileSync(join(root, 'first.txt'), 'before first\n');
    const secondOptions = { ...optionsFor(root, ['second.txt']), taskIssue: 1003 };
    const secondBaseline = await captureBaseline(secondOptions);
    writeFileSync(join(root, 'second.txt'), 'after second\n');
    const secondTask = await captureTask({
      ...secondOptions,
      baselinePath: secondBaseline.path,
    });
    writeFileSync(join(root, 'first.txt'), 'after first\n');
    await captureBaseline({
      ...optionsFor(root, ['first.txt', 'second.txt']),
      taskIssue: 1004,
      priorTaskRecords: [secondTask.path, firstTask.path],
    });
    throw new Error('capture unexpectedly accepted reordered prior task records');
  }
  throw new Error(`unknown fixture: ${fixture}`);
}

main(process.argv.slice(2)).catch(error => {
  process.stderr.write(`${error.message}\n`);
  process.exitCode = 1;
});
