#!/usr/bin/env node
// Discord Ferry - Claude Code second-opinion reviewer.
// Runs `claude -p` (default claude-opus-4-8) as a cross-host fallback reviewer for
// /df-ship step 4 and /df-chunk-review step 3, returning findings in Ferry's own
// `code_review_findings` schema. See ADR-023.
//
// The caller pipes the review payload on stdin: the `git diff` for a whole-branch review, or
// the full changed files followed by their diff for a chunk review. The instruction (focus text)
// is passed with --focus. On any Claude failure this exits non-zero so the caller can fall back
// to the next reviewer in the chain.
//
// Usage:
//   git diff origin/main | node claude-review.mjs --focus "API: rate limits, retry logic" --title "chunk 2"
//   node claude-review.mjs --self-test

import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';

const DEFAULT_MODEL = 'claude-opus-4-8';

// Ferry's canonical review schema, identical to codex-review.mjs.
// Kept in sync with .claude/skills/df-chunk-review/SKILL.md step 3.
// Claude's --json-schema accepts a bare JSON Schema object, same shape as Codex's --output-schema.
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

function parseArgs(argv) {
  const args = { mode: 'whole-branch', focus: '', title: '', model: DEFAULT_MODEL, selfTest: false };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    switch (a) {
      case '--self-test': args.selfTest = true; break;
      case '--mode': args.mode = argv[++i] ?? args.mode; break;
      case '--focus': args.focus = argv[++i] ?? ''; break;
      case '--title': args.title = argv[++i] ?? ''; break;
      case '--model': args.model = argv[++i] ?? DEFAULT_MODEL; break;
      default:
        process.stderr.write(`claude-review: unknown argument "${a}"\n`);
        process.exit(2);
    }
  }
  return args;
}

function buildPrompt(args) {
  const label = args.mode === 'chunk' ? 'chunk review' : 'whole-branch review';
  const titleLine = args.title ? `\nUnder review: ${args.title}` : '';
  const focusLine = args.focus ? `\nReview focus: ${args.focus}` : '';
  return [
    `You are a code reviewer performing a ${label} for the Discord Ferry project.`,
    titleLine,
    focusLine,
    '',
    REVIEW_DIRECTIVES,
    '',
    'The changed code follows below.',
  ].join('\n');
}

// Build a safe failure reason. Never echo an execFileSync error's .message/.stderr/.stdout:
// those can carry child output (diff text, tokens). Report only structural metadata for a child
// failure, and our own thrown messages (JSON parse, schema mismatch) verbatim, since we author them.
function failureReason(err) {
  if (!err) return 'unknown error';
  if (err.code === 'ENOENT') return 'claude CLI not found';
  if (typeof err.status === 'number' || err.signal) {
    const parts = [];
    if (typeof err.status === 'number') parts.push(`status ${err.status}`);
    if (err.signal) parts.push(`signal ${err.signal}`);
    return `claude -p failed (${parts.join(', ')})`;
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

function runClaude(args, payload) {
  const prompt = buildPrompt(args) + '\n' + payload;
  const cliArgs = [
    '-p',
    '--model', args.model,
    '--json-schema', JSON.stringify(FINDINGS_SCHEMA),
    '--dangerously-skip-permissions',
    '--allowedTools', '',
    '--append-system-prompt', PROJECT_CONTEXT,
  ];
  // No internal timeout: the bash timeout is the only guard (see ADR-023).
  // --allowedTools '' grants NO tools at all: not Bash, not Write, not Read, not WebFetch. A
  // prompt-injection in the diff cannot read files or exfiltrate credentials. This is stronger
  // than --disallowedTools with a partial denylist, which leaves read and network tools available.
  // --dangerously-skip-permissions only suppresses the interactive prompt; it does not constrain
  // the tool surface on its own.
  // Capture stdout; ignore stderr so Claude diagnostics never reach our error output
  // (ADR-014: tokens and diff text must not leak into errors).
  const raw = execFileSync('claude', cliArgs, {
    input: prompt,
    encoding: 'utf8',
    maxBuffer: 10 * 1024 * 1024,
    stdio: ['pipe', 'pipe', 'ignore'],
  });
  // Claude -p with --json-schema returns the constrained JSON directly (bare JSON, no envelope).
  // Wrap the parse so a SyntaxError (which embeds a snippet of the raw input) never leaks child
  // stdout into our error output.
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch {
    throw new Error('claude returned non-JSON stdout');
  }
  if (!isValidResult(parsed)) {
    // Claude enforces the schema via --json-schema, but do not trust that alone: a degraded CLI
    // or a non-strict response could still parse as JSON. Reject anything off-shape so the caller
    // falls back rather than acting on a malformed review.
    throw new Error('claude response did not match the findings schema');
  }
  return parsed;
}

function selfTest() {
  const checks = [];
  const record = (name, ok) => checks.push({ name, ok });

  const a = parseArgs(['--focus', 'Security: token handling', '--title', 'chunk 1', '--mode', 'chunk']);
  record('parseArgs focus', a.focus === 'Security: token handling');
  record('parseArgs title', a.title === 'chunk 1');
  record('parseArgs mode', a.mode === 'chunk');
  record('parseArgs model default', a.model === DEFAULT_MODEL);

  const prompt = buildPrompt(a);
  record('prompt has focus', prompt.includes('Security: token handling'));
  record('prompt has title', prompt.includes('chunk 1'));
  record('prompt names chunk mode', prompt.includes('chunk review'));
  record('prompt does not embed project context', !prompt.includes('core/engine.py never imports'));

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
  record('failureReason: missing binary', failureReason({ code: 'ENOENT' }) === 'claude CLI not found');
  record('failureReason: exit status only', failureReason({ status: 23, stderr: 'SENSITIVE' }) === 'claude -p failed (status 23)');
  record('failureReason: does not leak stderr', !failureReason({ status: 1, stderr: 'SENSITIVE', message: 'x SENSITIVE y' }).includes('SENSITIVE'));

  const failed = checks.filter((c) => !c.ok);
  for (const c of checks) process.stderr.write(`  ${c.ok ? 'ok  ' : 'FAIL'} ${c.name}\n`);
  if (failed.length) {
    process.stderr.write(`claude-review self-test: ${failed.length} failure(s)\n`);
    process.exit(1);
  }
  process.stderr.write('claude-review self-test: all checks passed\n');
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
    result = runClaude(args, payload);
  } catch (err) {
    process.stderr.write(`claude-review: ${failureReason(err)}\n`);
    process.exit(1);
  }
  process.stdout.write(JSON.stringify(result));
}

main();
