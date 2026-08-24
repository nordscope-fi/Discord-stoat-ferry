#!/usr/bin/env node
// Discord Ferry — Codex second-opinion reviewer.
// Runs `codex exec` (default gpt-5.6-sol, high effort) as the primary code reviewer for
// /df-ship step 4 and /df-chunk-review step 3, returning findings in Ferry's own
// `code_review_findings` schema. See ADR-023.
//
// The caller pipes the review payload on stdin: the `git diff` for a whole-branch review, or
// the full changed files followed by their diff for a chunk review. The instruction (focus text)
// is passed with --focus. On any Codex failure this exits non-zero so the caller can fall back
// to the Mistral MCP call.
//
// Usage:
//   git diff origin/main | node codex-review.mjs --focus "API: rate limits, retry logic" --title "chunk 2"
//   node codex-review.mjs --self-test

import { execFileSync } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const DEFAULT_MODEL = 'gpt-5.6-sol';
const DEFAULT_EFFORT = 'high';

// Ferry's canonical review schema, unwrapped from Mistral's response_format envelope.
// Kept in sync with .claude/skills/df-chunk-review/SKILL.md step 3.
// OpenAI structured outputs run in strict mode: every object needs additionalProperties:false,
// and every property must appear in `required` (optional fields use a nullable type instead).
const FINDINGS_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    findings: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        properties: {
          severity: { type: 'string', enum: ['critical', 'important', 'minor'] },
          category: {
            type: 'string',
            enum: ['security', 'correctness', 'performance', 'maintainability'],
          },
          file: { type: 'string' },
          line: { type: ['integer', 'null'] },
          description: { type: 'string' },
          suggestion: { type: 'string' },
          verification: {
            type: 'object',
            additionalProperties: false,
            properties: {
              command: { type: 'string' },
              confirms_if: { type: 'string' },
              refutes_if: { type: 'string' },
            },
            required: ['command', 'confirms_if', 'refutes_if'],
          },
        },
        required: ['severity', 'category', 'file', 'line', 'description', 'suggestion', 'verification'],
      },
    },
    summary: { type: 'string' },
    confidence: { type: 'string', enum: ['high', 'medium', 'low'] },
  },
  required: ['findings', 'summary', 'confidence'],
};

// The project context. Kept in sync with the system_prompt block in
// .claude/skills/df-chunk-review/SKILL.md step 3.
const PROJECT_CONTEXT = `Discord Ferry migrates a Discord server export to Stoat (a Revolt fork). Python 3.11+, aiohttp,
Click, Rich, NiceGUI, pytest.

Conventions that matter for review:
- core/engine.py never imports gui.py or cli.py. Everything crossing that boundary is an event.
- Every public function is fully typed. mypy runs in strict mode.
- Tokens never reach logs, errors, state files or report.json. Redaction lives in the logging
  Formatter, not a Filter.
- Ferry is single-user and single-process. Findings that assume concurrent multi-user access
  are false positives here.
- Stoat rate limits: 5 requests per 10 seconds on /servers, 15 per 10 seconds per channel.
  X-RateLimit-Reset-After is milliseconds, unlike Discord's identically named header.
- Every finding must carry a verification command that would actually distinguish the finding
  being true from it being false. It will be run.`;

const REVIEW_DIRECTIVES = `Focus on the diff and use any full files provided for context. Report only what you are
confident about. Return an empty findings array if the diff is clean. Every finding must carry a
verification command that distinguishes the finding being true from being false; the command will
be run by the caller, so you never need to run it yourself.`;

const DESIGN_DIRECTIVES = `Review the design or plan document. Judge feasibility, fit with the project context above,
missing edge cases, and whether the tasks are atomic enough to plan and verify on their own.
Report only what you are confident about, and return an empty findings array when the design is
sound. Set file to the document path or the section name. Every finding must carry a verification
command that distinguishes the finding being true from being false; the command will be run by the
caller, so you never need to run it yourself.`;

function parseArgs(argv) {
  const args = { mode: 'whole-branch', focus: '', title: '', model: DEFAULT_MODEL, effort: DEFAULT_EFFORT, selfTest: false };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    switch (a) {
      case '--self-test': args.selfTest = true; break;
      case '--mode': args.mode = argv[++i] ?? args.mode; break;
      case '--focus': args.focus = argv[++i] ?? ''; break;
      case '--title': args.title = argv[++i] ?? ''; break;
      case '--model': args.model = argv[++i] ?? DEFAULT_MODEL; break;
      case '--effort': args.effort = argv[++i] ?? DEFAULT_EFFORT; break;
      default:
        process.stderr.write(`codex-review: unknown argument "${a}"\n`);
        process.exit(2);
    }
  }
  return args;
}

function buildInstruction(args) {
  const labels = { chunk: 'chunk review', design: 'design review' };
  const label = labels[args.mode] ?? 'whole-branch review';
  const titleLine = args.title ? `\nUnder review: ${args.title}` : '';
  const focusLine = args.focus ? `\nReview focus: ${args.focus}` : '';
  const directives = args.mode === 'design' ? DESIGN_DIRECTIVES : REVIEW_DIRECTIVES;
  const payloadLine = args.mode === 'design'
    ? 'The design document follows on stdin.'
    : 'The changed code follows on stdin.';
  return [
    `You are a code reviewer performing a ${label} for the Discord Ferry project.`,
    '',
    PROJECT_CONTEXT,
    titleLine,
    focusLine,
    '',
    directives,
    '',
    payloadLine,
  ].join('\n');
}

// Build a safe failure reason. Never echo an execFileSync error's `.message`/`.stderr`/`.stdout`:
// those can carry child output (diff text, tokens). Report only structural metadata for a child
// failure, and our own thrown messages (JSON parse, schema mismatch) verbatim, since we author them.
function failureReason(err) {
  if (!err) return 'unknown error';
  if (err.code === 'ENOENT') return 'codex CLI not found';
  if (typeof err.status === 'number' || err.signal) {
    const parts = [];
    if (typeof err.status === 'number') parts.push(`status ${err.status}`);
    if (err.signal) parts.push(`signal ${err.signal}`);
    return `codex exec failed (${parts.join(', ')})`;
  }
  return err.message ?? String(err);
}

const VALID_SEVERITIES = ['critical', 'important', 'minor'];
const VALID_CATEGORIES = ['security', 'correctness', 'performance', 'maintainability'];
const VALID_CONFIDENCES = ['high', 'medium', 'low'];

function isValidResult(r) {
  if (!r || typeof r !== 'object') return false;
  if (typeof r.summary !== 'string') return false;
  if (typeof r.confidence !== 'string' || !VALID_CONFIDENCES.includes(r.confidence)) return false;
  if (!Array.isArray(r.findings)) return false;
  for (const f of r.findings) {
    if (!f || typeof f !== 'object') return false;
    if (typeof f.severity !== 'string' || !VALID_SEVERITIES.includes(f.severity)) return false;
    if (typeof f.category !== 'string' || !VALID_CATEGORIES.includes(f.category)) return false;
    for (const k of ['file', 'description', 'suggestion']) {
      if (typeof f[k] !== 'string') return false;
    }
    if (f.line !== null && typeof f.line !== 'number') return false;
    const v = f.verification;
    if (!v || typeof v !== 'object') return false;
    for (const k of ['command', 'confirms_if', 'refutes_if']) {
      if (typeof v[k] !== 'string') return false;
    }
  }
  return true;
}

function runCodex(args, payload) {
  const tmp = mkdtempSync(join(tmpdir(), 'ferry-codex-review-'));
  const schemaFile = join(tmp, 'schema.json');
  const lastMsgFile = join(tmp, 'last-message.json');
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
      '-o', lastMsgFile,
      buildInstruction(args),
    ];
    // The -o file is the only output we read. Ignore the child's stdout/stderr so a verbose
    // reasoning trace can never overflow execFileSync's buffer (turning a good review into a
    // failure), and so Codex diagnostics can never reach our own error output (ADR-014: tokens
    // and diff text must not leak into errors).
    execFileSync('codex', cliArgs, {
      input: payload,
      encoding: 'utf8',
      timeout: 600000,
      stdio: ['pipe', 'ignore', 'ignore'],
    });
    const raw = readFileSync(lastMsgFile, 'utf8').trim();
    const parsed = JSON.parse(raw); // throws on non-JSON → caller falls back
    if (!isValidResult(parsed)) {
      // Codex enforces the schema server-side, but do not trust that alone: a degraded CLI or a
      // non-strict response could still parse as JSON. Reject anything off-shape so the caller
      // falls back rather than acting on a malformed review.
      throw new Error('codex response did not match the findings schema');
    }
    return parsed;
  } finally {
    rmSync(tmp, { recursive: true, force: true });
  }
}

function selfTest() {
  const checks = [];
  const record = (name, ok) => checks.push({ name, ok });

  const a = parseArgs(['--focus', 'Security: token handling', '--title', 'chunk 1', '--mode', 'chunk']);
  record('parseArgs focus', a.focus === 'Security: token handling');
  record('parseArgs title', a.title === 'chunk 1');
  record('parseArgs mode', a.mode === 'chunk');
  record('parseArgs model default', a.model === DEFAULT_MODEL);
  record('parseArgs effort default', a.effort === DEFAULT_EFFORT);

  const instruction = buildInstruction(a);
  record('instruction has project context', instruction.includes('core/engine.py never imports'));
  record('instruction has focus', instruction.includes('Security: token handling'));
  record('instruction has title', instruction.includes('chunk 1'));
  record('instruction names chunk mode', instruction.includes('chunk review'));

  const d = parseArgs(['--mode', 'design', '--focus', 'Task atomicity', '--title', 'a design doc']);
  record('parseArgs design mode', d.mode === 'design');
  const designInstruction = buildInstruction(d);
  record('design instruction names design mode', designInstruction.includes('design review'));
  record('design instruction carries design directives', designInstruction.includes('design or plan document'));
  record('design instruction names the payload', designInstruction.includes('design document follows on stdin'));

  // Schema is a bare JSON Schema (no Mistral response_format envelope).
  record('schema is unwrapped', FINDINGS_SCHEMA.type === 'object' && !('json_schema' in FINDINGS_SCHEMA));
  record('schema requires verification', FINDINGS_SCHEMA.properties.findings.items.required.includes('verification'));
  const verReq = FINDINGS_SCHEMA.properties.findings.items.properties.verification.required;
  record('verification requires command/confirms/refutes',
    verReq.includes('command') && verReq.includes('confirms_if') && verReq.includes('refutes_if'));

  // Schema-shape validation of the parsed result.
  const goodResult = { summary: 's', confidence: 'high', findings: [{
    severity: 'minor', category: 'correctness', file: 'x.py', line: null,
    description: 'd', suggestion: 's', verification: { command: 'c', confirms_if: 'a', refutes_if: 'b' },
  }] };
  record('isValidResult accepts a clean result', isValidResult(goodResult) === true);
  record('isValidResult accepts empty findings', isValidResult({ summary: 's', confidence: 'high', findings: [] }) === true);
  record('isValidResult rejects a finding missing fields', isValidResult({ summary: 's', confidence: 'high', findings: [{ foo: 'bar' }] }) === false);
  record('isValidResult rejects missing summary', isValidResult({ confidence: 'high', findings: [] }) === false);
  record('isValidResult rejects non-object', isValidResult('nope') === false);
  record('isValidResult rejects invalid severity', isValidResult({ summary: 's', confidence: 'high', findings: [{ ...goodResult.findings[0], severity: 'urgent' }] }) === false);
  record('isValidResult rejects invalid confidence', isValidResult({ summary: 's', confidence: 'certain', findings: [] }) === false);
  record('isValidResult rejects invalid category', isValidResult({ summary: 's', confidence: 'high', findings: [{ ...goodResult.findings[0], category: 'other' }] }) === false);
  record('isValidResult rejects non-number line', isValidResult({ summary: 's', confidence: 'high', findings: [{ ...goodResult.findings[0], line: '42' }] }) === false);

  // Failure reasons never echo child output.
  record('failureReason: missing binary', failureReason({ code: 'ENOENT' }) === 'codex CLI not found');
  record('failureReason: exit status only', failureReason({ status: 23, stderr: 'SENSITIVE' }) === 'codex exec failed (status 23)');
  record('failureReason: does not leak stderr', !failureReason({ status: 1, stderr: 'SENSITIVE', message: 'x SENSITIVE y' }).includes('SENSITIVE'));

  const failed = checks.filter((c) => !c.ok);
  for (const c of checks) process.stderr.write(`  ${c.ok ? 'ok  ' : 'FAIL'} ${c.name}\n`);
  if (failed.length) {
    process.stderr.write(`codex-review self-test: ${failed.length} failure(s)\n`);
    process.exit(1);
  }
  process.stderr.write('codex-review self-test: all checks passed\n');
  process.exit(0);
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args.selfTest) return selfTest();

  const payload = readFileSync(0, 'utf8');
  if (!payload.trim()) {
    // Nothing to review: emit an empty, clean result rather than failing.
    process.stdout.write(JSON.stringify({ findings: [], summary: 'empty diff', confidence: 'high' }));
    process.exit(0);
  }

  let result;
  try {
    result = runCodex(args, payload);
  } catch (err) {
    process.stderr.write(`codex-review: ${failureReason(err)}\n`);
    process.exit(1);
  }
  process.stdout.write(JSON.stringify(result));
}

main();
