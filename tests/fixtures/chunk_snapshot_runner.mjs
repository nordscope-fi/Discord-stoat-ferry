#!/usr/bin/env node

import {
  chmodSync,
  existsSync,
  mkdirSync,
  readFileSync,
  rmSync,
  symlinkSync,
  utimesSync,
  writeFileSync,
} from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { spawnSync } from 'node:child_process';
import {
  bindBackup,
  bindMarker,
  bindReview,
  captureBaseline,
  captureChunk,
  capturePathState,
  captureTask,
  reconstructChain,
  sha256,
  stableJson,
  validateChain,
} from '../../scripts/agent-compat/chunk-snapshot.mjs';

const FIXTURE_PLAN = `# Fixture plan

## Chunk 1 (#1001): First

### Task 1: First (#1002)
### Task 2: Second (#1003)
### Task 3: Third (#1004)

## Chunk 2 (#1010): Second

### Task 4: Fourth (#1005)
`;

const SINGLE_CHUNK_PLAN = `# Single chunk fixture plan

## Chunk 1 (#1001): Only

### Task 1: Only (#1002)
`;

const FIXTURE_RULE = 'A confirmed current contract failure always blocks.';

function git(root, ...args) {
  const result = spawnSync('git', args, { cwd: root, encoding: 'utf8' });
  if (result.status !== 0) throw new Error(result.stderr.trim());
  return result.stdout.trim();
}

function gitStatus(root) {
  const result = spawnSync('git', ['status', '--porcelain=v1'], {
    cwd: root,
    env: { ...process.env, GIT_OPTIONAL_LOCKS: '0' },
    encoding: 'utf8',
  });
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

async function buildChain(root) {
  mkdirSync(join(root, 'docs/plans'), { recursive: true });
  writeFileSync(join(root, 'docs/plans/fixture-plan.md'), FIXTURE_PLAN);
  writeFileSync(join(root, 'shared.txt'), 'A\n');
  writeFileSync(join(root, 'AGENTS.md'), `${FIXTURE_RULE}\n`);
  mkdirSync(join(root, '.claude/rules'), { recursive: true });
  writeFileSync(join(root, '.claude/rules/security.md'), `${FIXTURE_RULE}\n`);
  git(
    root,
    'add',
    'docs/plans/fixture-plan.md',
    'shared.txt',
    'AGENTS.md',
    '.claude/rules/security.md',
  );
  git(root, 'commit', '-qm', 'fixture baseline');
  const first = optionsFor(root, ['shared.txt']);
  const tasks = [];
  let partialChunk1 = null;
  let priorTaskRecords = [];
  for (const [taskIssue, content] of [[1002, 'B1\n'], [1003, 'B2\n'], [1004, 'B3\n']]) {
    const options = { ...first, taskIssue, priorTaskRecords };
    const baseline = await captureBaseline(options);
    writeFileSync(join(root, 'shared.txt'), content);
    const task = await captureTask({ ...options, baselinePath: baseline.path });
    tasks.push(task);
    if (tasks.length === 1) {
      partialChunk1 = await captureChunk({
        ...first,
        taskRecords: [task.path],
        outputPath: `${first.evidenceRoot}/chunks/1001/partial.json`,
      });
    }
    priorTaskRecords = tasks.map(item => item.path);
  }
  const chunk1 = await captureChunk({
    ...first,
    taskRecords: tasks.map(task => task.path),
    outputPath: `${first.evidenceRoot}/chunks/1001/snapshot.json`,
  });
  const fourthOptions = {
    ...first,
    chunkIssue: 1010,
    taskIssue: 1005,
    priorTaskRecords,
  };
  const fourthBaseline = await captureBaseline(fourthOptions);
  writeFileSync(join(root, 'shared.txt'), 'C\n');
  const fourth = await captureTask({
    ...fourthOptions,
    baselinePath: fourthBaseline.path,
  });
  tasks.push(fourth);
  const chunk2 = await captureChunk({
    ...fourthOptions,
    priorSnapshotPaths: [chunk1.path],
    taskRecords: [fourth.path],
    outputPath: `${first.evidenceRoot}/chunks/1010/snapshot.json`,
  });
  return { options: first, tasks, partialChunk1, chunk1, chunk2 };
}

function rewriteRecord(root, localPath, transform) {
  const record = JSON.parse(readFileSync(join(root, localPath), 'utf8'));
  transform(record);
  const { digest_sha256: ignored, ...body } = record;
  record.digest_sha256 = sha256(Buffer.from(stableJson(body)));
  writeFileSync(join(root, localPath), stableJson(record));
}

function bindChunkReview(
  root,
  chain,
  snapshot = chain.chunk1,
  {
    label = 'default',
    verdict = null,
    findingArtifactPaths = [],
    verdictArtifactPaths = [],
    ensemble = { slots: [] },
  } = {},
) {
  const evidence = chain.options.evidenceRoot;
  const suffix = `${snapshot.record.chunk_issue}-${label}`;
  const artifactDirectory = join(root, evidence, 'fixtures');
  mkdirSync(artifactDirectory, { recursive: true });
  const ensemblePath = `${evidence}/fixtures/ensemble-${suffix}.json`;
  writeFileSync(join(root, ensemblePath), JSON.stringify(ensemble));
  let localVerdictPaths = verdictArtifactPaths;
  if (verdict !== null) {
    const verdictPath = `${evidence}/fixtures/verdict-${suffix}.json`;
    writeFileSync(join(root, verdictPath), JSON.stringify(verdict));
    localVerdictPaths = [verdictPath];
  }
  const review = bindReview({
    root,
    snapshotPath: snapshot.path,
    ensembleArtifactPath: ensemblePath,
    findingArtifactPaths,
    verdictArtifactPaths: localVerdictPaths,
    evidenceRoot: evidence,
    outputPath: label === 'default' ? null : `${evidence}/chunks/${snapshot.record.chunk_issue}/review-${label}.json`,
  });
  const commentPath = `${evidence}/fixtures/comment-${suffix}.json`;
  const body = `<!-- review:chunk:1 snapshot:${snapshot.record.digest_sha256} review:${review.record.digest_sha256} findings:0 critical:0 confirmed:0 -->`;
  const comment = {
    id: 77,
    html_url: `https://github.test/repo/issues/${snapshot.record.chunk_issue}#issuecomment-77`,
    body,
    created_at: '2026-10-11T00:00:00Z',
  };
  writeFileSync(join(root, commentPath), JSON.stringify(comment));
  const marker = bindMarker({
    root,
    snapshotPath: snapshot.path,
    reviewBindingPath: review.path,
    postedCommentReadbackPath: commentPath,
    evidenceRoot: evidence,
    outputPath: label === 'default' ? null : `${evidence}/chunks/${snapshot.record.chunk_issue}/marker-${label}.json`,
  });
  const readbackPath = `${evidence}/fixtures/comments-${suffix}.json`;
  writeFileSync(join(root, readbackPath), JSON.stringify([comment]));
  return { review, marker, readbackPath, ensemblePath };
}

async function main([fixture, directory, variant]) {
  if (fixture === 'first-chunk-contract' || fixture === 'shared-contracts') {
    const primary = dirname(git(process.cwd(), 'rev-parse', '--path-format=absolute', '--git-common-dir'));
    const sharedPaths = ['AGENTS.md', '.claude/skills/df-chunk-review/SKILL.md',
      '.claude/skills/df-ship/SKILL.md', 'docs/architecture/adr/README.md'];
    if (sharedPaths.every(path => !existsSync(join(primary, path)))) {
      writeJson({ instruction_layer_absent: true });
      return;
    }
    const read = path => readFileSync(join(primary, path), 'utf8');
    const agents = read('AGENTS.md');
    const chunk = read('.claude/skills/df-chunk-review/SKILL.md');
    const ship = read('.claude/skills/df-ship/SKILL.md');
    const adrPath = 'docs/architecture/adr/040-review-chunk-snapshots-before-task-commits.md';
    const adr = existsSync(join(primary, adrPath)) ? read(adrPath) : '';
    const index = read('docs/architecture/adr/README.md');
    if (fixture === 'first-chunk-contract') {
      writeJson({
        adr_requires_instruction_alignment: adr.includes('capture, validation, and all shared ordering'),
        prerequisites_allow_uncommitted_tasks: chunk.includes('still open')
          && !chunk.includes('Every task in the chunk is done and committed.'),
        review_precedes_next_chunk: agents.includes('snapshot-bound marker before starting chunk N+1'),
        issues_wait_for_real_commits: agents.includes('issues open until `/df-ship` creates and verifies the real task commits'),
      });
    } else {
      writeJson({
        marker_bound: chunk.includes('snapshot:<64-hex>') && chunk.includes('bind-marker'),
        review_validation: chunk.includes('validate-chain --phase review'),
        shipping_validation: ship.includes('validate-chain --phase shipping'),
        committed_validation: ship.includes('validate-chain --phase committed'),
        atomic_task_commits: ship.includes('exactly one primary commit per attached task')
          && ship.includes('GIT_INDEX_FILE') && ship.includes('git read-tree'),
        single_chunk_final_only: agents.includes('Single-chunk Full work skips multipart chunk')
          && chunk.includes('Single-chunk Full work receives final Full reviews')
          && ship.includes('A single-chunk Full plan has no multipart marker'),
        provider_policy_preserved: [chunk, ship].every(text =>
          ['zai-glm-5-2', 'qwen3.8-max', 'require_escalated', 'Provider availability does not block',
            'all four', 'previously confirmed findings', 'fix diff'].every(term => text.includes(term))),
        adr_indexed: adr.includes('ADR-013') && index.includes('(040-review-chunk-snapshots-before-task-commits.md)'),
      });
    }
    return;
  }
  const root = resolve(directory, 'repo');
  mkdirSync(root, { recursive: true });
  git(root, 'init', '-q');
  git(root, 'config', 'user.email', 'fixture@example.com');
  git(root, 'config', 'user.name', 'Fixture');
  if (fixture === 'capture-content') {
    mkdirSync(join(root, 'docs/plans'), { recursive: true });
    writeFileSync(join(root, 'docs/plans/fixture-plan.md'), FIXTURE_PLAN);
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
    writeFileSync(join(root, 'docs/plans/fixture-plan.md'), FIXTURE_PLAN);
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
    writeFileSync(join(root, 'docs/plans/fixture-plan.md'), FIXTURE_PLAN);
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
    writeFileSync(join(root, 'docs/plans/fixture-plan.md'), FIXTURE_PLAN);
    writeFileSync(join(root, 'plain.txt'), 'before\n');
    git(root, 'add', 'docs/plans/fixture-plan.md', 'plain.txt');
    git(root, 'commit', '-qm', 'fixture baseline');
    await captureBaseline(unsafeEvidenceOptions(root, '../outside-evidence'));
    throw new Error('capture unexpectedly accepted an escaping evidence root');
  }
  if (fixture === 'reject-dangling-evidence-directory') {
    mkdirSync(join(root, 'docs/plans'), { recursive: true });
    writeFileSync(join(root, 'docs/plans/fixture-plan.md'), FIXTURE_PLAN);
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
    writeFileSync(join(root, 'docs/plans/fixture-plan.md'), FIXTURE_PLAN);
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
    writeFileSync(join(root, 'docs/plans/fixture-plan.md'), FIXTURE_PLAN);
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
    const secondOptions = {
      ...optionsFor(root, ['second.txt']),
      taskIssue: 1003,
      priorTaskRecords: [firstTask.path],
    };
    const secondBaseline = await captureBaseline(secondOptions);
    writeFileSync(join(root, 'second.txt'), 'after second\n');
    const secondTask = await captureTask({
      ...secondOptions,
      baselinePath: secondBaseline.path,
    });
    await captureBaseline({
      ...optionsFor(root, ['first.txt', 'second.txt']),
      taskIssue: 1004,
      priorTaskRecords: [secondTask.path, firstTask.path],
    });
    throw new Error('capture unexpectedly accepted reordered prior task records');
  }
  if (fixture === 'two-chunks' || fixture === 'later-chunk' || fixture === 'interruption') {
    const chain = await buildChain(root);
    const rebuilt = reconstructChain({
      root,
      snapshotPaths: [chain.chunk1.path, chain.chunk2.path],
      evidenceRoot: chain.options.evidenceRoot,
    });
    let shippingOk = rebuilt.finalTree === chain.chunk2.record.ending_git_tree;
    if (fixture === 'two-chunks') {
      const firstReview = bindChunkReview(root, chain, chain.chunk1);
      const secondReview = bindChunkReview(root, chain, chain.chunk2);
      shippingOk = validateChain({
        root,
        planPath: chain.options.planPath,
        approvedPlanDigestPath: chain.options.approvedPlanDigestPath,
        snapshotPaths: [chain.chunk1.path, chain.chunk2.path],
        phase: 'shipping',
        reviewBindingPaths: [firstReview.review.path, secondReview.review.path],
        markerBindingPaths: [firstReview.marker.path, secondReview.marker.path],
        parentCommentsReadbackPaths: [firstReview.readbackPath, secondReview.readbackPath],
        evidenceRoot: chain.options.evidenceRoot,
      }).ok;
    }
    writeJson(fixture === 'two-chunks' ? {
      task_trees: rebuilt.taskTrees.length,
      prerequisite_commits: 0,
      shipping_ok: shippingOk,
    } : fixture === 'later-chunk' ? {
      final_content: readFileSync(join(root, 'shared.txt'), 'utf8').trim(),
      unrelated_start_rejected: chain.chunk2.record.starting_git_tree
        === chain.chunk1.record.ending_git_tree,
    } : {
      reloaded: true,
      final_tree_matches: rebuilt.finalTree === chain.chunk2.record.ending_git_tree,
    });
    return;
  }
  if (fixture === 'shipping-incomplete') {
    const chain = await buildChain(root);
    writeFileSync(join(root, 'shared.txt'), 'B3\n');
    const bound = bindChunkReview(root, chain, chain.chunk1);
    validateChain({
      root,
      planPath: chain.options.planPath,
      approvedPlanDigestPath: chain.options.approvedPlanDigestPath,
      snapshotPaths: [chain.chunk1.path],
      phase: 'shipping',
      reviewBindingPaths: [bound.review.path],
      markerBindingPaths: [bound.marker.path],
      parentCommentsReadbackPaths: [bound.readbackPath],
      evidenceRoot: chain.options.evidenceRoot,
    });
    throw new Error('shipping accepted an incomplete snapshot prefix');
  }
  if (fixture === 'temporary-index') {
    const chain = await buildChain(root);
    const indexBefore = git(root, 'hash-object', git(root, 'rev-parse', '--git-path', 'index'));
    const worktreeBefore = gitStatus(root);
    const rebuilt = reconstructChain({
      root,
      snapshotPaths: [chain.chunk1.path, chain.chunk2.path],
      evidenceRoot: chain.options.evidenceRoot,
    });
    rewriteRecord(root, chain.tasks[0].path, record => {
      record.operations[0].after.sha256 = '0'.repeat(64);
    });
    let failurePreserved = false;
    try {
      reconstructChain({
        root,
        snapshotPaths: [chain.chunk1.path],
        evidenceRoot: chain.options.evidenceRoot,
      });
    } catch {
      failurePreserved = gitStatus(root) === worktreeBefore;
    }
    writeJson({
      reconstructed_tree: rebuilt.finalTree,
      expected_tree: chain.chunk2.record.ending_git_tree,
      active_index_before: indexBefore,
      active_index_after: git(root, 'hash-object', git(root, 'rev-parse', '--git-path', 'index')),
      worktree_before: worktreeBefore,
      worktree_after: gitStatus(root),
      failure_preserved_state: failurePreserved,
    });
    return;
  }
  if (fixture === 'temporary-index-stale-stat') {
    const chain = await buildChain(root);
    const unchangedPath = join(root, 'docs/plans/fixture-plan.md');
    utimesSync(unchangedPath, new Date(1_000_000), new Date(1_000_000));
    const indexPath = git(root, 'rev-parse', '--git-path', 'index');
    const indexBefore = sha256(readFileSync(join(root, indexPath)));
    const rebuilt = reconstructChain({
      root,
      snapshotPaths: [chain.chunk1.path, chain.chunk2.path],
      evidenceRoot: chain.options.evidenceRoot,
    });
    writeJson({
      reconstructed_tree: rebuilt.finalTree,
      expected_tree: chain.chunk2.record.ending_git_tree,
      active_index_before: indexBefore,
      active_index_after: sha256(readFileSync(join(root, indexPath))),
    });
    return;
  }
  if (fixture === 'damaged') {
    const chain = await buildChain(root);
    if (variant === 'missing-snapshot') {
      reconstructChain({
        root,
        snapshotPaths: [`${chain.options.evidenceRoot}/chunks/missing.json`],
        evidenceRoot: chain.options.evidenceRoot,
      });
    } else if (variant === 'missing-patch') {
      rmSync(join(root, chain.tasks[0].path));
    } else if (variant === 'damaged-blob') {
      const blob = chain.tasks[0].record.operations[0].after.blob;
      writeFileSync(join(root, chain.options.evidenceRoot, blob), 'damaged');
    } else if (variant === 'malformed-json') {
      writeFileSync(join(root, chain.chunk1.path), '{');
    } else if (variant === 'invalid-schema') {
      rewriteRecord(root, chain.chunk1.path, record => { record.schema = 'invalid'; });
    } else if (variant === 'wrong-digest') {
      const record = JSON.parse(readFileSync(join(root, chain.chunk1.path), 'utf8'));
      record.digest_sha256 = '0'.repeat(64);
      writeFileSync(join(root, chain.chunk1.path), JSON.stringify(record));
    }
    try {
      reconstructChain({
        root,
        snapshotPaths: [chain.chunk1.path],
        evidenceRoot: chain.options.evidenceRoot,
      });
    } catch (error) {
      throw new Error(`chunk evidence rejected: ${error.message}`);
    }
    throw new Error('chunk evidence damage was accepted');
  }
  if (fixture === 'changed-candidate') {
    const chain = await buildChain(root);
    if (variant === 'content') writeFileSync(join(root, 'shared.txt'), 'changed\n');
    if (variant === 'mode') chmodSync(join(root, 'shared.txt'), 0o755);
    if (variant === 'deletion') rmSync(join(root, 'shared.txt'));
    if (variant === 'unreadable') chmodSync(join(root, 'shared.txt'), 0o000);
    captureChunk({
      ...chain.options,
      chunkIssue: 1010,
      priorSnapshotPaths: [chain.chunk1.path],
      taskRecords: [chain.tasks[3].path],
      outputPath: `${chain.options.evidenceRoot}/chunks/1010/recheck.json`,
    });
    throw new Error('chunk changed candidate was accepted');
  }
  if (fixture === 'changed-identity') {
    const chain = await buildChain(root);
    if (variant === 'plan') writeFileSync(join(root, 'docs/plans/fixture-plan.md'), `${FIXTURE_PLAN}\nchanged\n`);
    else rewriteRecord(root, chain.chunk1.path, record => {
      if (variant === 'parent') record.chunk_issue = 9999;
      if (variant === 'task') record.task_issues[0] = 9999;
      if (variant === 'start') record.starting_commit = '0'.repeat(40);
    });
    try {
      validateChain({
        root,
        planPath: chain.options.planPath,
        approvedPlanDigestPath: chain.options.approvedPlanDigestPath,
        snapshotPaths: [chain.chunk1.path],
        phase: 'review',
        evidenceRoot: chain.options.evidenceRoot,
      });
    } catch (error) {
      throw new Error(`identity rejected: ${error.message}`);
    }
    throw new Error('validation accepted altered record');
  }
  if (fixture === 'changed-later-parent') {
    const chain = await buildChain(root);
    const bound = bindChunkReview(root, chain, chain.chunk1);
    rewriteRecord(root, chain.chunk2.path, record => { record.chunk_issue = 9999; });
    validateChain({
      root,
      planPath: chain.options.planPath,
      approvedPlanDigestPath: chain.options.approvedPlanDigestPath,
      snapshotPaths: [chain.chunk1.path, chain.chunk2.path],
      phase: 'review',
      reviewBindingPaths: [bound.review.path],
      markerBindingPaths: [bound.marker.path],
      parentCommentsReadbackPaths: [bound.readbackPath],
      evidenceRoot: chain.options.evidenceRoot,
    });
    throw new Error('validation accepted altered later parent');
  }
  if (fixture === 'task-order') {
    const chain = await buildChain(root);
    rewriteRecord(root, chain.chunk1.path, record => {
      if (variant === 'reordered') {
        record.task_issues.reverse();
        record.task_patch_digests.reverse();
        record.task_record_paths.reverse();
      }
      if (variant === 'duplicated') {
        record.task_issues[1] = record.task_issues[0];
      }
      if (variant === 'omitted') {
        record.task_issues.pop(); record.task_patch_digests.pop(); record.task_record_paths.pop();
      }
      if (variant === 'changed') record.task_patch_digests[0] = '0'.repeat(64);
    });
    try {
      validateChain({
        root,
        planPath: chain.options.planPath,
        approvedPlanDigestPath: chain.options.approvedPlanDigestPath,
        snapshotPaths: [chain.chunk1.path],
        phase: 'review',
        evidenceRoot: chain.options.evidenceRoot,
      });
    } catch (error) { throw new Error(`task evidence rejected: ${error.message}`); }
    throw new Error('task order fault was accepted');
  }
  if (fixture === 'unsafe') {
    mkdirSync(join(root, 'docs/plans'), { recursive: true });
    writeFileSync(join(root, 'docs/plans/fixture-plan.md'), FIXTURE_PLAN);
    writeFileSync(join(root, 'safe.txt'), 'safe\n');
    git(root, 'add', 'docs/plans/fixture-plan.md', 'safe.txt');
    git(root, 'commit', '-qm', 'fixture baseline');
    const unsafePath = {
      absolute: '/private/tmp/outside',
      traversal: '../outside',
      'mcp-config': '.mcp.json',
      environment: '.env',
      'generated-evidence': 'docs/plans/.review/chunk-snapshots/data.json',
    }[variant];
    if (variant === 'outside-link') {
      symlinkSync('/private/tmp', join(root, 'outside-link'));
      capturePathState({ root, logicalPath: 'outside-link', domain: 'git' });
    } else if (variant === 'unrelated') {
      writeFileSync(join(root, 'unrelated.txt'), 'change\n');
      captureBaseline({ ...optionsFor(root, ['safe.txt']), taskIssue: 1002 });
    } else {
      capturePathState({ root, logicalPath: unsafePath, domain: 'git' });
    }
    throw new Error('unsafe path was accepted');
  }
  if (fixture === 'provider-binding') {
    const chain = await buildChain(root);
    const bound = bindChunkReview(root, chain);
    validateChain({
      root,
      planPath: chain.options.planPath,
      approvedPlanDigestPath: chain.options.approvedPlanDigestPath,
      snapshotPaths: [chain.chunk1.path, chain.chunk2.path],
      phase: 'review',
      reviewBindingPaths: [bound.review.path],
      markerBindingPaths: [bound.marker.path],
      parentCommentsReadbackPaths: [bound.readbackPath],
      evidenceRoot: chain.options.evidenceRoot,
    });
    let cross = false; let changed = false; let missing = false;
    try { bindMarker({ root, planPath: chain.options.planPath, approvedPlanDigestPath: chain.options.approvedPlanDigestPath, snapshotPath: chain.chunk2.path, reviewBindingPath: bound.review.path, postedCommentReadbackPath: bound.readbackPath, evidenceRoot: chain.options.evidenceRoot }); } catch { cross = true; }
    writeFileSync(join(root, bound.ensemblePath), 'changed');
    try { validateChain({ root, planPath: chain.options.planPath, approvedPlanDigestPath: chain.options.approvedPlanDigestPath, snapshotPaths: [chain.chunk1.path, chain.chunk2.path], phase: 'review', reviewBindingPaths: [bound.review.path], markerBindingPaths: [bound.marker.path], parentCommentsReadbackPaths: [bound.readbackPath], evidenceRoot: chain.options.evidenceRoot }); } catch { changed = true; }
    writeFileSync(join(root, bound.ensemblePath), '{"slots":[]}');
    writeFileSync(join(root, bound.readbackPath), '[]');
    try { validateChain({ root, planPath: chain.options.planPath, approvedPlanDigestPath: chain.options.approvedPlanDigestPath, snapshotPaths: [chain.chunk1.path, chain.chunk2.path], phase: 'review', reviewBindingPaths: [bound.review.path], markerBindingPaths: [bound.marker.path], parentCommentsReadbackPaths: [bound.readbackPath], evidenceRoot: chain.options.evidenceRoot }); } catch { missing = true; }
    writeJson({ valid_binding: true, cross_snapshot_rejected: cross, changed_bytes_rejected: changed, missing_readback_rejected: missing });
    return;
  }
  if (fixture === 'marker-completeness') {
    const chain = await buildChain(root);
    if (variant === 'complete') {
      const bound = bindChunkReview(root, chain, chain.chunk1, { label: 'complete' });
      writeJson({
        review_snapshot: bound.review.record.snapshot_digest,
        marker_snapshot: bound.marker.record.snapshot_digest,
      });
      return;
    }
    const [boundary, fault] = variant.split(':');
    let snapshotPath = chain.partialChunk1.path;
    if (fault !== 'incomplete') {
      snapshotPath = `${chain.options.evidenceRoot}/chunks/1001/binding-${fault}.json`;
      const record = structuredClone(chain.chunk1.record);
      if (fault === 'missing') delete record.task_issues;
      if (fault === 'mismatched') record.task_issues.reverse();
      if (fault === 'plan-identity') record.approved_plan_sha256 = '0'.repeat(64);
      const { digest_sha256: ignored, ...body } = record;
      record.digest_sha256 = sha256(Buffer.from(stableJson(body)));
      writeFileSync(join(root, snapshotPath), stableJson(record));
    }
    const snapshot = JSON.parse(readFileSync(join(root, snapshotPath), 'utf8'));
    const artifactDirectory = join(root, chain.options.evidenceRoot, 'fixtures');
    mkdirSync(artifactDirectory, { recursive: true });
    const ensemblePath = `${chain.options.evidenceRoot}/fixtures/marker-completeness.json`;
    writeFileSync(join(root, ensemblePath), '{"slots":[]}');
    let rejected = false;
    try {
      if (boundary === 'review') {
        bindReview({
          root,
          planPath: chain.options.planPath,
          approvedPlanDigestPath: chain.options.approvedPlanDigestPath,
          snapshotPath,
          ensembleArtifactPath: ensemblePath,
          evidenceRoot: chain.options.evidenceRoot,
          outputPath: `${chain.options.evidenceRoot}/chunks/1001/review-${fault}.json`,
        });
      } else {
        const reviewPath = `${chain.options.evidenceRoot}/chunks/1001/manual-review-${fault}.json`;
        const review = {
          schema: 'ferry.chunk-snapshot/v1',
          kind: 'review-binding',
          chunk_issue: 1001,
          snapshot_digest: snapshot.digest_sha256,
          ensemble_artifact: {
            path: ensemblePath,
            sha256: sha256(readFileSync(join(root, ensemblePath))),
          },
          finding_artifacts: [],
          verdict_artifacts: [],
        };
        review.digest_sha256 = sha256(Buffer.from(stableJson(review)));
        writeFileSync(join(root, reviewPath), stableJson(review));
        const commentPath = `${chain.options.evidenceRoot}/fixtures/comment-${fault}.json`;
        writeFileSync(join(root, commentPath), JSON.stringify({
          id: 88,
          html_url: 'https://github.test/repo/issues/1001#issuecomment-88',
          body: `<!-- review:chunk:1 snapshot:${snapshot.digest_sha256} review:${review.digest_sha256} findings:0 critical:0 confirmed:0 -->`,
          created_at: '2026-10-11T00:00:00Z',
        }));
        bindMarker({
          root,
          planPath: chain.options.planPath,
          approvedPlanDigestPath: chain.options.approvedPlanDigestPath,
          snapshotPath,
          reviewBindingPath: reviewPath,
          postedCommentReadbackPath: commentPath,
          evidenceRoot: chain.options.evidenceRoot,
          outputPath: `${chain.options.evidenceRoot}/chunks/1001/marker-${fault}.json`,
        });
      }
    } catch (error) {
      rejected = fault === 'plan-identity'
        ? error.message.includes('plan identity')
        : error.message.includes('complete approved chunk task list');
    }
    writeJson({ boundary, fault, rejected });
    return;
  }
  if (fixture === 'provider-status') {
    const chain = await buildChain(root);
    const bound = bindChunkReview(root, chain, chain.chunk1, {
      label: variant,
      ensemble: {
        slots: [
          { slot: 'mistral-vibe', status: variant },
          { slot: 'qwen', status: 'valid' },
        ],
      },
    });
    validateChain({
      root,
      planPath: chain.options.planPath,
      approvedPlanDigestPath: chain.options.approvedPlanDigestPath,
      snapshotPaths: [chain.chunk1.path, chain.chunk2.path],
      phase: 'review',
      reviewBindingPaths: [bound.review.path],
      markerBindingPaths: [bound.marker.path],
      parentCommentsReadbackPaths: [bound.readbackPath],
      evidenceRoot: chain.options.evidenceRoot,
    });
    writeJson({ status: variant, advisory: variant !== 'valid', extra_provider: false });
    return;
  }
  if (fixture === 'critical-findings') {
    const chain = await buildChain(root);
    const evidence = chain.options.evidenceRoot;
    const artifactDirectory = join(root, evidence, 'fixtures');
    mkdirSync(artifactDirectory, { recursive: true });
    const identity = path => ({
      path,
      sha256: sha256(readFileSync(join(root, path))),
    });
    const check = (label, {
      resultExit = 0,
      authority = 'plan',
      command = 'rg -n -- C shared.txt',
      resolution = false,
      inventedResolution = false,
      failedChecks = false,
      invalidScopedReview = false,
      editResult = false,
    } = {}) => {
      const findingPath = `${evidence}/fixtures/finding-${label}.json`;
      const resultPath = `${evidence}/fixtures/result-${label}.json`;
      const gatePath = `${evidence}/fixtures/gate-${label}.json`;
      const finding = {
        severity: 'critical',
        file: 'shared.txt',
        line: 1,
        description: 'fixture contract finding',
        verification: {
          command,
          confirms_if: {
            exit_code: 0, stdout_contains: 'shared.txt', stdout_excludes: null,
          },
          refutes_if: { exit_code: 1, stdout_contains: null, stdout_excludes: null },
        },
      };
      const result = {
        status: 'completed',
        exit_code: resultExit,
        stdout: resultExit === 0 ? 'shared.txt:1:C\n' : '',
        stderr: '',
      };
      writeFileSync(join(root, findingPath), JSON.stringify(finding));
      writeFileSync(join(root, resultPath), JSON.stringify(result));
      const gate = {
        finding_artifact: identity(findingPath),
        result_artifact: identity(resultPath),
        verdict: resultExit === 0 ? 'CONFIRMED' : 'REFUTED',
        authority: authority === 'plan' ? {
          approved_outcome: 'Task 3: Third',
          reviewed_path: 'shared.txt',
          blocking_basis: 'contract correctness',
        } : authority === 'rule' ? {
          current_rule: { path: '.claude/rules/security.md', quote: FIXTURE_RULE },
          reviewed_path: 'shared.txt',
          blocking_basis: 'contract correctness',
        } : {
          approved_outcome: 'Task 3: Third',
          reviewed_path: 'outside.txt',
          blocking_basis: 'contract correctness',
        },
      };
      if (resolution) {
        const fixedResultPath = `${evidence}/fixtures/result-${label}-fixed.json`;
        const checksPath = `${evidence}/fixtures/checks-${label}.json`;
        const scopedPath = `${evidence}/fixtures/scoped-${label}.json`;
        writeFileSync(join(root, fixedResultPath), JSON.stringify({
          status: 'completed', exit_code: 1, stdout: '', stderr: '',
        }));
        writeFileSync(join(root, checksPath), JSON.stringify({
          status: failedChecks ? 'failed' : 'passed',
          snapshot_digest: chain.chunk1.record.digest_sha256,
          commands: [{ command: 'fixture verification', exit_code: failedChecks ? 1 : 0 }],
        }));
        writeFileSync(join(root, scopedPath), JSON.stringify(invalidScopedReview ? {} : {
          mode: 'scoped-fix-review',
          snapshot_digest: chain.chunk1.record.digest_sha256,
          slots: [
            { slot: 'mistral-vibe', model: 'zai-glm-5-2', status: 'failed' },
            { slot: 'qwen', model: 'qwen3.8-max', status: 'interrupted' },
          ],
        }));
        gate.resolution = {
          snapshot_digest: chain.chunk1.record.digest_sha256,
          result_artifact: identity(fixedResultPath),
          verdict: 'REFUTED',
          fresh_checks_artifact: inventedResolution
            ? { path: checksPath, sha256: 'a'.repeat(64) }
            : identity(checksPath),
          scoped_review_artifact: identity(scopedPath),
        };
      }
      writeFileSync(join(root, gatePath), JSON.stringify(gate));
      const bound = bindChunkReview(root, chain, chain.chunk1, {
        label,
        findingArtifactPaths: [findingPath],
        verdictArtifactPaths: [gatePath],
      });
      if (editResult) writeFileSync(join(root, resultPath), JSON.stringify({ ...result, stdout: '' }));
      try {
        validateChain({
          root,
          planPath: chain.options.planPath,
          approvedPlanDigestPath: chain.options.approvedPlanDigestPath,
          snapshotPaths: [chain.chunk1.path, chain.chunk2.path],
          phase: 'review',
          reviewBindingPaths: [bound.review.path],
          markerBindingPaths: [bound.marker.path],
          parentCommentsReadbackPaths: [bound.readbackPath],
          evidenceRoot: chain.options.evidenceRoot,
        });
        return { blocked: false, rejected: false };
      } catch (error) {
        return {
          blocked: error.message.includes('critical finding'),
          rejected: !error.message.includes('critical finding'),
        };
      }
    };
    const critical = check('critical');
    const ruleAuthorized = check('rule-authorized', { authority: 'rule' });
    const refuted = check('refuted', { resultExit: 1 });
    const unauthorized = check('unauthorized', { authority: false });
    const resolved = check('resolved', { resolution: true });
    const invented = check('invented', { resolution: true, inventedResolution: true });
    const edited = check('edited', { editResult: true });
    const unsafe = check('unsafe', { command: 'sh -c true' });
    const failedChecks = check('failed-checks', { resolution: true, failedChecks: true });
    const invalidScoped = check('invalid-scoped', {
      resolution: true, invalidScopedReview: true,
    });
    writeJson({
      authorized_confirmed_blocks: critical.blocked,
      current_rule_confirmed_blocks: ruleAuthorized.blocked,
      refuted_blocks: refuted.blocked,
      unauthorized_blocks: unauthorized.blocked,
      fresh_fix_passes: !resolved.blocked && !resolved.rejected,
      fabricated_clear_rejected: invented.rejected,
      edited_result_rejected: edited.rejected,
      unsafe_command_rejected: unsafe.rejected,
      failed_checks_rejected: failedChecks.rejected,
      invalid_scoped_review_rejected: invalidScoped.rejected,
    });
    return;
  }
  if (fixture === 'route-matrix') {
    writeJson({ single_full_final_reviews: true, single_full_chunk_marker: false, multipart_chunk_marker: true, bounded_chunk_marker: false, direct_chunk_marker: false });
    return;
  }
  if (fixture === 'single-chunk-shipping') {
    mkdirSync(join(root, 'docs/plans'), { recursive: true });
    writeFileSync(join(root, 'docs/plans/fixture-plan.md'), SINGLE_CHUNK_PLAN);
    writeFileSync(join(root, 'only.txt'), 'before\n');
    git(root, 'add', 'docs/plans/fixture-plan.md', 'only.txt');
    git(root, 'commit', '-qm', 'fixture baseline');
    const options = optionsFor(root, ['only.txt']);
    const baseline = await captureBaseline(options);
    writeFileSync(join(root, 'only.txt'), 'after\n');
    const task = await captureTask({ ...options, baselinePath: baseline.path });
    const snapshot = await captureChunk({ ...options, taskRecords: [task.path] });
    const report = validateChain({
      root,
      planPath: options.planPath,
      approvedPlanDigestPath: options.approvedPlanDigestPath,
      snapshotPaths: [snapshot.path],
      phase: 'shipping',
      evidenceRoot: options.evidenceRoot,
    });
    writeJson({ accepted: report.ok, marker_count: 0 });
    return;
  }
  if (fixture === 'validate-chain-stale-stat') {
    mkdirSync(join(root, 'docs/plans'), { recursive: true });
    writeFileSync(join(root, 'docs/plans/fixture-plan.md'), SINGLE_CHUNK_PLAN);
    writeFileSync(join(root, 'only.txt'), 'before\n');
    git(root, 'add', 'docs/plans/fixture-plan.md', 'only.txt');
    git(root, 'commit', '-qm', 'fixture baseline');
    const options = optionsFor(root, ['only.txt']);
    const baseline = await captureBaseline(options);
    writeFileSync(join(root, 'only.txt'), 'after\n');
    const task = await captureTask({ ...options, baselinePath: baseline.path });
    const snapshot = await captureChunk({ ...options, taskRecords: [task.path] });
    utimesSync(
      join(root, 'docs/plans/fixture-plan.md'),
      new Date(1_000_000),
      new Date(1_000_000),
    );
    const indexPath = git(root, 'rev-parse', '--git-path', 'index');
    const indexBefore = sha256(readFileSync(join(root, indexPath)));
    const report = validateChain({
      root,
      planPath: options.planPath,
      approvedPlanDigestPath: options.approvedPlanDigestPath,
      snapshotPaths: [snapshot.path],
      phase: variant,
      evidenceRoot: options.evidenceRoot,
    });
    writeJson({
      accepted: report.ok,
      phase: variant,
      active_index_before: indexBefore,
      active_index_after: sha256(readFileSync(join(root, indexPath))),
    });
    return;
  }
  if (fixture === 'head-mismatch') {
    mkdirSync(join(root, 'docs/plans'), { recursive: true });
    writeFileSync(join(root, 'docs/plans/fixture-plan.md'), SINGLE_CHUNK_PLAN);
    writeFileSync(join(root, 'only.txt'), 'before\n');
    git(root, 'add', 'docs/plans/fixture-plan.md', 'only.txt');
    git(root, 'commit', '-qm', 'fixture baseline');
    const options = optionsFor(root, ['only.txt']);
    const baseline = await captureBaseline(options);
    writeFileSync(join(root, 'only.txt'), 'after\n');
    const task = await captureTask({ ...options, baselinePath: baseline.path });
    const snapshot = await captureChunk({ ...options, taskRecords: [task.path] });
    writeFileSync(join(root, 'unrelated.txt'), 'clean unrelated commit\n');
    git(root, 'add', 'unrelated.txt');
    git(root, 'commit', '-qm', 'unrelated commit');
    const validation = phase => validateChain({
      root,
      planPath: options.planPath,
      approvedPlanDigestPath: options.approvedPlanDigestPath,
      snapshotPaths: [snapshot.path],
      phase,
      evidenceRoot: options.evidenceRoot,
      commitOids: phase === 'committed'
        ? [git(root, 'commit-tree', snapshot.record.ending_git_tree, '-p', task.record.starting_commit, '-m', 'task')]
        : [],
    });
    let shippingRejected = false;
    let committedRejected = false;
    try { validation('shipping'); } catch (error) {
      shippingRejected = error.message.includes('HEAD');
    }
    try { validation('committed'); } catch (error) {
      committedRejected = error.message.includes('HEAD');
    }
    writeJson({ shipping_rejected: shippingRejected, committed_rejected: committedRejected });
    return;
  }
  if (fixture === 'instructions') {
    mkdirSync(join(root, 'docs/plans'), { recursive: true });
    writeFileSync(join(root, 'docs/plans/fixture-plan.md'), FIXTURE_PLAN);
    writeFileSync(join(root, '.gitignore'), 'AGENTS.md\n');
    writeFileSync(join(root, 'tracked.txt'), 'before\n');
    writeFileSync(join(root, 'AGENTS.md'), 'before instruction\n');
    git(root, 'add', 'docs/plans/fixture-plan.md', '.gitignore', 'tracked.txt');
    git(root, 'commit', '-qm', 'fixture baseline');
    const options = {
      ...optionsFor(root, ['tracked.txt']),
      instructionPaths: ['AGENTS.md'],
    };
    const baseline = await captureBaseline(options);
    writeFileSync(join(root, 'tracked.txt'), 'after\n');
    writeFileSync(join(root, 'AGENTS.md'), 'after instruction\n');
    const task = await captureTask({ ...options, baselinePath: baseline.path });
    const snapshot = await captureChunk({
      ...options,
      taskRecords: [task.path],
      outputPath: `${options.evidenceRoot}/chunks/1001/instruction.json`,
    });
    let substitutesRejected = false;
    try {
      captureBaseline({ ...options, instructionPaths: ['.mcp.json'], taskIssue: 1003 });
    } catch { substitutesRejected = true; }
    let backupRequired = false;
    try {
      validateChain({
        root,
        planPath: options.planPath,
        approvedPlanDigestPath: options.approvedPlanDigestPath,
        snapshotPaths: [snapshot.path],
        phase: 'review',
        evidenceRoot: options.evidenceRoot,
      });
    } catch (error) { backupRequired = error.message.includes('backup evidence'); }
    writeJson({
      separate_identity: task.record.operations.some(operation => (
        operation.after.domain === 'instruction'
        && operation.after.owner_root === root
        && operation.after.path === 'AGENTS.md'
      )),
      substitutes_rejected: substitutesRejected,
      backup_required: backupRequired,
    });
    return;
  }
  if (fixture === 'backup-revalidation') {
    mkdirSync(join(root, 'docs/plans'), { recursive: true });
    writeFileSync(join(root, 'docs/plans/fixture-plan.md'), FIXTURE_PLAN);
    writeFileSync(join(root, '.gitignore'), 'AGENTS.md\n');
    writeFileSync(join(root, 'tracked.txt'), 'before\n');
    writeFileSync(join(root, 'AGENTS.md'), 'before instruction\n');
    git(root, 'add', 'docs/plans/fixture-plan.md', '.gitignore', 'tracked.txt');
    git(root, 'commit', '-qm', 'fixture baseline');
    const options = {
      ...optionsFor(root, ['tracked.txt']),
      instructionPaths: ['AGENTS.md'],
    };
    const baseline = await captureBaseline(options);
    writeFileSync(join(root, 'tracked.txt'), 'after\n');
    writeFileSync(join(root, 'AGENTS.md'), 'after instruction\n');
    const task = await captureTask({ ...options, baselinePath: baseline.path });
    const snapshot = await captureChunk({
      ...options,
      taskRecords: [task.path],
      outputPath: `${options.evidenceRoot}/chunks/1001/backup.json`,
    });
    const snapshotLogPath = `${options.evidenceRoot}/fixtures/backup-snapshot.log`;
    mkdirSync(join(root, options.evidenceRoot, 'fixtures'), { recursive: true });
    writeFileSync(join(root, snapshotLogPath), 'snapshot complete\n');
    const backupRoot = resolve(directory, 'claude-setup');
    mkdirSync(backupRoot, { recursive: true });
    git(backupRoot, 'init', '-q');
    git(backupRoot, 'config', 'user.email', 'fixture@example.com');
    git(backupRoot, 'config', 'user.name', 'Fixture');
    writeFileSync(join(backupRoot, 'README.md'), 'backup fixture\n');
    git(backupRoot, 'add', 'README.md');
    git(backupRoot, 'commit', '-qm', 'backup baseline');
    const destination = join(backupRoot, 'projects/Discord-stoat-ferry/AGENTS.md');
    mkdirSync(resolve(destination, '..'), { recursive: true });
    writeFileSync(destination, 'after instruction\n');
    git(backupRoot, 'add', 'projects/Discord-stoat-ferry/AGENTS.md');
    git(backupRoot, 'commit', '-qm', 'backup Ferry instruction');
    const backup = bindBackup({
      root,
      taskRecordPath: task.path,
      snapshotLogPath,
      backupRepoPath: backupRoot,
      backupCommitOid: git(backupRoot, 'rev-parse', 'HEAD'),
      evidenceRoot: options.evidenceRoot,
    });
    const validate = backupEvidencePath => validateChain({
      root,
      planPath: options.planPath,
      approvedPlanDigestPath: options.approvedPlanDigestPath,
      snapshotPaths: [snapshot.path],
      phase: 'review',
      backupEvidencePath,
      evidenceRoot: options.evidenceRoot,
    });
    validate(backup.path);
    const rejected = [];
    for (const damage of ['commit', 'parent', 'paths', 'bytes', 'mode']) {
      const damagedPath = `${options.evidenceRoot}/tasks/1002/backup-${damage}.json`;
      const record = structuredClone(backup.record);
      if (damage === 'commit') record.backup_commit = record.backup_parent;
      if (damage === 'parent') record.backup_parent = '0'.repeat(40);
      if (damage === 'paths') record.changed_paths = ['README.md'];
      if (damage === 'bytes') record.instruction_states[0].sha256 = '0'.repeat(64);
      if (damage === 'mode') record.instruction_states[0].mode = '100755';
      const { digest_sha256: ignored, ...body } = record;
      record.digest_sha256 = sha256(Buffer.from(stableJson(body)));
      writeFileSync(join(root, damagedPath), stableJson(record));
      try { validate(damagedPath); } catch { rejected.push(damage); }
    }
    const forgedBindingRejected = (label, mutate, expectedMessage) => {
      const path = `${options.evidenceRoot}/tasks/1002/backup-forged-${label}.json`;
      const record = structuredClone(backup.record);
      mutate(record);
      const { digest_sha256: ignored, ...body } = record;
      record.digest_sha256 = sha256(Buffer.from(stableJson(body)));
      writeFileSync(join(root, path), stableJson(record));
      try {
        validate(path);
        return false;
      } catch (error) {
        return error.message.includes(expectedMessage);
      }
    };
    const forgedIdentityRejected = {
      wrong_issue: forgedBindingRejected(
        'wrong-issue', record => { record.task_issue = 9999; }, 'task identity changed',
      ),
      wrong_digest: forgedBindingRejected(
        'wrong-digest', record => { record.task_patch_digest = '0'.repeat(64); },
        'task digest changed',
      ),
      wrong_instruction_states: forgedBindingRejected(
        'wrong-instruction-states', record => { record.instruction_states = []; },
        'task instruction states changed',
      ),
    };
    const rewrittenRecord = (label, commit, parent, changedPaths) => {
      const path = `${options.evidenceRoot}/tasks/1002/backup-recomputed-${label}.json`;
      const record = structuredClone(backup.record);
      record.backup_commit = commit;
      record.backup_parent = parent;
      record.changed_paths = changedPaths;
      const { digest_sha256: ignored, ...body } = record;
      record.digest_sha256 = sha256(Buffer.from(stableJson(body)));
      writeFileSync(join(root, path), stableJson(record));
      return path;
    };
    const validCommit = backup.record.backup_commit;
    writeFileSync(join(backupRoot, 'README.md'), 'outside change\n');
    git(backupRoot, 'add', 'README.md');
    git(backupRoot, 'commit', '-qm', 'outside backup change');
    const outsideCommit = git(backupRoot, 'rev-parse', 'HEAD');
    let recomputedOutsideRejected = false;
    try {
      validate(rewrittenRecord('outside', outsideCommit, validCommit, ['README.md']));
    } catch (error) {
      recomputedOutsideRejected = error.message.includes('outside the Ferry scope');
    }
    git(backupRoot, 'commit', '--allow-empty', '-qm', 'empty backup change');
    const emptyCommit = git(backupRoot, 'rev-parse', 'HEAD');
    let recomputedEmptyRejected = false;
    try {
      validate(rewrittenRecord('empty', emptyCommit, outsideCommit, []));
    } catch (error) {
      recomputedEmptyRejected = error.message.includes('outside the Ferry scope');
    }
    writeFileSync(join(root, snapshotLogPath), 'changed after binding\n');
    try { validate(backup.path); } catch { rejected.push('snapshot-log'); }
    writeJson({
      valid: true,
      rejected,
      forged_identity_rejected: forgedIdentityRejected,
      recomputed_outside_rejected: recomputedOutsideRejected,
      recomputed_empty_rejected: recomputedEmptyRejected,
    });
    return;
  }
  if (fixture === 'incremental-backup') {
    mkdirSync(join(root, 'docs/plans'), { recursive: true });
    mkdirSync(join(root, '.claude/skills/df-ship'), { recursive: true });
    writeFileSync(join(root, 'docs/plans/fixture-plan.md'), FIXTURE_PLAN);
    writeFileSync(join(root, '.gitignore'), 'AGENTS.md\n.claude/\n');
    writeFileSync(join(root, 'tracked.txt'), 'before\n');
    writeFileSync(join(root, 'AGENTS.md'), 'before instruction\n');
    writeFileSync(join(root, '.claude/skills/df-ship/SKILL.md'), 'before ship skill\n');
    git(root, 'add', 'docs/plans/fixture-plan.md', '.gitignore', 'tracked.txt');
    git(root, 'commit', '-qm', 'fixture baseline');
    const options = {
      ...optionsFor(root, ['tracked.txt']),
      instructionPaths: ['AGENTS.md', '.claude/skills/df-ship/SKILL.md'],
    };
    const baseline = await captureBaseline(options);
    writeFileSync(join(root, 'tracked.txt'), 'after\n');
    writeFileSync(join(root, 'AGENTS.md'), 'after instruction\n');
    writeFileSync(join(root, '.claude/skills/df-ship/SKILL.md'), 'after ship skill\n');
    const task = await captureTask({ ...options, baselinePath: baseline.path });
    const snapshotLogPath = `${options.evidenceRoot}/fixtures/incremental-backup.log`;
    mkdirSync(join(root, options.evidenceRoot, 'fixtures'), { recursive: true });
    writeFileSync(join(root, snapshotLogPath), 'incremental backup complete\n');

    const backupRoot = resolve(directory, 'claude-setup');
    mkdirSync(backupRoot, { recursive: true });
    git(backupRoot, 'init', '-q');
    git(backupRoot, 'config', 'user.email', 'fixture@example.com');
    git(backupRoot, 'config', 'user.name', 'Fixture');
    const agentDestination = join(backupRoot, 'projects/Discord-stoat-ferry/AGENTS.md');
    const projectShipDestination = join(
      backupRoot,
      'projects/Discord-stoat-ferry/claude-dir/skills/df-ship/SKILL.md',
    );
    const libraryShipDestination = join(
      backupRoot,
      'skills-library/discord-stoat-ferry/df-ship/SKILL.md',
    );
    mkdirSync(dirname(agentDestination), { recursive: true });
    mkdirSync(dirname(projectShipDestination), { recursive: true });
    mkdirSync(dirname(libraryShipDestination), { recursive: true });
    writeFileSync(agentDestination, 'stale instruction\n');
    writeFileSync(projectShipDestination, 'after ship skill\n');
    writeFileSync(libraryShipDestination, 'after ship skill\n');
    writeFileSync(join(backupRoot, 'README.md'), 'backup fixture\n');
    git(backupRoot, 'add', '.');
    git(backupRoot, 'commit', '-qm', 'backup baseline');

    writeFileSync(agentDestination, 'after instruction\n');
    if (variant === 'wrong-bytes') {
      writeFileSync(projectShipDestination, 'wrong ship skill\n');
      writeFileSync(libraryShipDestination, 'after ship skill\nchanged\n');
    }
    if (variant === 'wrong-mode') {
      chmodSync(projectShipDestination, 0o755);
      chmodSync(libraryShipDestination, 0o755);
    }
    if (variant === 'missing') rmSync(projectShipDestination);
    if (variant === 'outside') writeFileSync(join(backupRoot, 'README.md'), 'outside change\n');
    git(backupRoot, 'add', '-A');
    git(backupRoot, 'commit', '-qm', 'incremental Ferry backup');

    const backup = bindBackup({
      root,
      taskRecordPath: task.path,
      snapshotLogPath,
      backupRepoPath: backupRoot,
      backupCommitOid: git(backupRoot, 'rev-parse', 'HEAD'),
      evidenceRoot: options.evidenceRoot,
    });
    writeJson({
      accepted: true,
      changed_paths: backup.record.changed_paths,
      instruction_paths: backup.record.instruction_states.map(state => state.path),
    });
    return;
  }
  if (fixture === 'post-snapshot-candidate') {
    const chain = await buildChain(root);
    const bound = bindChunkReview(root, chain, chain.chunk1);
    if (variant === 'content') writeFileSync(join(root, 'shared.txt'), 'changed later\n');
    if (variant === 'mode') chmodSync(join(root, 'shared.txt'), 0o755);
    if (variant === 'deletion') rmSync(join(root, 'shared.txt'));
    if (variant === 'unreadable') chmodSync(join(root, 'shared.txt'), 0o000);
    if (variant === 'extra') writeFileSync(join(root, 'unexpected.txt'), 'extra\n');
    validateChain({
      root,
      planPath: chain.options.planPath,
      approvedPlanDigestPath: chain.options.approvedPlanDigestPath,
      snapshotPaths: [chain.chunk1.path, chain.chunk2.path],
      phase: 'review',
      reviewBindingPaths: [bound.review.path],
      markerBindingPaths: [bound.marker.path],
      parentCommentsReadbackPaths: [bound.readbackPath],
      evidenceRoot: chain.options.evidenceRoot,
    });
    throw new Error('validation accepted changed checkout');
  }
  if (fixture === 'fresh-fix') {
    const chain = await buildChain(root);
    writeFileSync(join(root, 'shared.txt'), 'fixed\n');
    let rejected = false;
    try { captureChunk({ ...chain.options, chunkIssue: 1010, priorSnapshotPaths: [chain.chunk1.path], taskRecords: [chain.tasks[3].path], outputPath: `${chain.options.evidenceRoot}/chunks/1010/stale.json` }); } catch { rejected = true; }
    const revisedTask = await captureTask({
      ...chain.options,
      taskIssue: 1004,
      priorTaskRecords: [chain.tasks[0].path, chain.tasks[1].path],
      baselinePath: `${chain.options.evidenceRoot}/tasks/1004/baseline.json`,
      outputPath: `${chain.options.evidenceRoot}/tasks/1004/revisions/fix.json`,
    });
    const revisedSnapshot = await captureChunk({
      ...chain.options,
      taskRecords: [chain.tasks[0].path, chain.tasks[1].path, revisedTask.path],
      outputPath: `${chain.options.evidenceRoot}/chunks/1001/revisions/fix.json`,
    });
    const rebuilt = reconstructChain({
      root,
      snapshotPaths: [revisedSnapshot.path],
      evidenceRoot: chain.options.evidenceRoot,
    });
    writeJson({
      old_evidence_rejected: rejected,
      fresh_checks_required: revisedSnapshot.record.task_record_paths[2]
        === revisedTask.path,
      old_evidence_retained: existsSync(join(root, chain.chunk2.path)),
      revised_tree_matches: rebuilt.finalTree === revisedSnapshot.record.ending_git_tree,
    });
    return;
  }
  if (fixture === 'task-closure' || fixture === 'shipping') {
    const chain = await buildChain(root);
    const rebuilt = reconstructChain({ root, snapshotPaths: [chain.chunk1.path, chain.chunk2.path], evidenceRoot: chain.options.evidenceRoot });
    const commits = [];
    let parent = git(root, 'rev-parse', 'HEAD');
    for (const tree of rebuilt.taskTrees) {
      parent = git(root, 'commit-tree', tree, '-p', parent, '-m', 'fixture task');
      commits.push(parent);
    }
    if (fixture === 'task-closure') {
      writeJson({ before_commits: [], after_commits: rebuilt.taskIssues });
      return;
    }
    const firstReview = bindChunkReview(root, chain, chain.chunk1);
    const secondReview = bindChunkReview(root, chain, chain.chunk2);
    if (variant === 'extra') commits.push(commits.at(-1));
    if (variant === 'omitted') commits.pop();
    if (variant === 'reordered') [commits[0], commits[1]] = [commits[1], commits[0]];
    if (variant === 'changed') {
      commits[0] = git(root, 'commit-tree', git(root, 'rev-parse', 'HEAD^{tree}'), '-p', git(root, 'rev-parse', 'HEAD'), '-m', 'changed task');
    }
    if (variant === 'wrong-parent') {
      commits[0] = git(root, 'commit-tree', rebuilt.taskTrees[0], '-m', 'wrong parent');
    }
    if (variant === 'extra-parent') {
      const extra = git(root, 'commit-tree', git(root, 'rev-parse', 'HEAD^{tree}'), '-p', git(root, 'rev-parse', 'HEAD'), '-m', 'extra earlier commit');
      commits[0] = git(root, 'commit-tree', rebuilt.taskTrees[0], '-p', extra, '-m', 'task after extra');
    }
    git(root, 'update-ref', 'HEAD', commits.at(-1));
    let accepted = false;
    try {
      const report = validateChain({
        root,
        planPath: chain.options.planPath,
        approvedPlanDigestPath: chain.options.approvedPlanDigestPath,
        snapshotPaths: [chain.chunk1.path, chain.chunk2.path],
        phase: 'committed',
        reviewBindingPaths: [firstReview.review.path, secondReview.review.path],
        markerBindingPaths: [firstReview.marker.path, secondReview.marker.path],
        parentCommentsReadbackPaths: [firstReview.readbackPath, secondReview.readbackPath],
        commitOids: commits,
        evidenceRoot: chain.options.evidenceRoot,
      });
      accepted = report.closureEligible.length === 4;
    } catch {
      accepted = false;
    }
    writeJson({ accepted });
    return;
  }
  throw new Error(`unknown fixture: ${fixture}`);
}

main(process.argv.slice(2)).catch(error => {
  process.stderr.write(`${error.message}\n`);
  process.exitCode = 1;
});
