#!/usr/bin/env node
// Discord Ferry Codex design-opinion reviewer.
// Runs `codex exec` in a read-only sandbox and returns Ferry's bare findings JSON.
// The fixed Vibe and Qwen slots own code-review gates. This adapter remains available for
// design critique and direct second opinions. See ADR-023 and ADR-027.
//
// Usage:
//   git diff origin/main | node codex-review.mjs --focus "API: rate limits" --title "chunk 2"
//   node codex-review.mjs --self-test

import { execFileSync } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import {
  FINDINGS_SCHEMA,
  buildReviewPrompt,
  parseJsonText,
  safeChildFailure,
  validateFindings,
} from './review-contract.mjs';

const DEFAULT_MODEL = 'gpt-5.6-sol';
const DEFAULT_EFFORT = 'high';

function parseArgs(argv) {
  const args = {
    mode: 'whole-branch',
    focus: '',
    title: '',
    model: DEFAULT_MODEL,
    effort: DEFAULT_EFFORT,
    selfTest: false,
  };
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    switch (argument) {
      case '--self-test':
        args.selfTest = true;
        break;
      case '--mode':
        args.mode = argv[++index] ?? args.mode;
        break;
      case '--focus':
        args.focus = argv[++index] ?? '';
        break;
      case '--title':
        args.title = argv[++index] ?? '';
        break;
      case '--model':
        args.model = argv[++index] ?? DEFAULT_MODEL;
        break;
      case '--effort':
        args.effort = argv[++index] ?? DEFAULT_EFFORT;
        break;
      default:
        process.stderr.write(`codex-review: unknown argument "${argument}"\n`);
        process.exit(2);
    }
  }
  return args;
}

function reviewPrompt(args) {
  return buildReviewPrompt({
    mode: args.mode,
    title: args.title,
    focus: args.focus,
    payloadLabel: args.mode === 'design' ? 'design document' : 'changed code',
  });
}

function runCodex(args, payload) {
  const temporaryDirectory = mkdtempSync(join(tmpdir(), 'ferry-codex-review-'));
  const schemaFile = join(temporaryDirectory, 'schema.json');
  const lastMessageFile = join(temporaryDirectory, 'last-message.json');
  try {
    writeFileSync(schemaFile, JSON.stringify(FINDINGS_SCHEMA));
    const cliArgs = [
      'exec',
      '-m', args.model,
      '-c', `model_reasoning_effort="${args.effort}"`,
      '-s', 'read-only',
      '--skip-git-repo-check',
      '--color', 'never',
      '--output-schema', schemaFile,
      '-o', lastMessageFile,
      reviewPrompt(args),
    ];
    // Read only the output file. Child diagnostics and reasoning never reach this process's output.
    execFileSync('codex', cliArgs, {
      input: payload,
      encoding: 'utf8',
      timeout: 600000,
      stdio: ['pipe', 'ignore', 'ignore'],
    });
    const raw = readFileSync(lastMessageFile, 'utf8').trim();
    const parsed = parseJsonText(raw, 'codex');
    if (!validateFindings(parsed)) {
      throw new Error('codex response did not match the findings schema');
    }
    return parsed;
  } finally {
    rmSync(temporaryDirectory, { recursive: true, force: true });
  }
}

function selfTest() {
  const checks = [];
  const record = (name, ok) => checks.push({ name, ok });

  const args = parseArgs([
    '--focus', 'Security: token handling',
    '--title', 'chunk 1',
    '--mode', 'chunk',
  ]);
  record('parseArgs focus', args.focus === 'Security: token handling');
  record('parseArgs title', args.title === 'chunk 1');
  record('parseArgs mode', args.mode === 'chunk');
  record('parseArgs model default', args.model === DEFAULT_MODEL);
  record('parseArgs effort default', args.effort === DEFAULT_EFFORT);

  const instruction = reviewPrompt(args);
  record('instruction has project context', instruction.includes('core/engine.py never imports'));
  record('instruction has focus', instruction.includes('Security: token handling'));
  record('instruction has title', instruction.includes('chunk 1'));
  record('instruction names chunk mode', instruction.includes('chunk review'));

  const designArgs = parseArgs([
    '--mode', 'design',
    '--focus', 'Task atomicity',
    '--title', 'a design doc',
  ]);
  record('parseArgs design mode', designArgs.mode === 'design');
  const designInstruction = reviewPrompt(designArgs);
  record('design instruction names design mode', designInstruction.includes('design review'));
  record(
    'design instruction carries design directives',
    designInstruction.includes('design or plan document'),
  );
  record(
    'design instruction names the payload',
    designInstruction.includes('design document follows on stdin'),
  );

  record(
    'schema is unwrapped',
    FINDINGS_SCHEMA.type === 'object' && !('json_schema' in FINDINGS_SCHEMA),
  );
  record(
    'schema requires verification',
    FINDINGS_SCHEMA.properties.findings.items.required.includes('verification'),
  );
  const goodResult = {
    summary: 'summary',
    confidence: 'high',
    findings: [{
      severity: 'minor',
      category: 'correctness',
      file: 'x.py',
      line: null,
      description: 'description',
      suggestion: 'suggestion',
      verification: {
        command: 'git status --short',
        confirms_if: {
          exit_code: 0,
          stdout_contains: 'present',
          stdout_excludes: null,
        },
        refutes_if: {
          exit_code: 0,
          stdout_contains: null,
          stdout_excludes: 'present',
        },
      },
    }],
  };
  record('validateFindings accepts a result', validateFindings(goodResult));
  record(
    'validateFindings accepts empty findings',
    validateFindings({ summary: 'summary', confidence: 'high', findings: [] }),
  );
  record(
    'validateFindings rejects an invalid severity',
    !validateFindings({
      ...goodResult,
      findings: [{ ...goodResult.findings[0], severity: 'urgent' }],
    }),
  );
  record(
    'safeChildFailure names missing binary',
    safeChildFailure('codex', { code: 'ENOENT' }) === 'codex executable not found',
  );
  record(
    'safeChildFailure hides child streams',
    !safeChildFailure('codex', {
      status: 23,
      stderr: 'SENSITIVE',
      stdout: 'SENSITIVE',
      message: 'SENSITIVE',
    }).includes('SENSITIVE'),
  );

  const failed = checks.filter((check) => !check.ok);
  for (const check of checks) {
    process.stderr.write(`  ${check.ok ? 'ok  ' : 'FAIL'} ${check.name}\n`);
  }
  if (failed.length) {
    process.stderr.write(`codex-review self-test: ${failed.length} failure(s)\n`);
    process.exit(1);
  }
  process.stderr.write('codex-review self-test: all checks passed\n');
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args.selfTest) return selfTest();

  const payload = readFileSync(0, 'utf8');
  if (!payload.trim()) {
    process.stdout.write(JSON.stringify({
      findings: [],
      summary: 'empty diff',
      confidence: 'high',
    }));
    return;
  }

  try {
    process.stdout.write(JSON.stringify(runCodex(args, payload)));
  } catch (error) {
    process.stderr.write(`codex-review: ${safeChildFailure('codex', error)}\n`);
    process.exit(1);
  }
}

main();
