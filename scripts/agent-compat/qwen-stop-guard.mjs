#!/usr/bin/env node
// Discord Ferry — Qwen Stop guard.
// Inline equivalent of ~/.claude/hooks/unfinished-guard.mjs, which reads Claude
// transcript shapes Qwen does not write. Same compensated disposition as the
// Codex adapter's stopGuard: block a stop that claims completion in the same
// breath as announcing unfinished work. Fail-open by contract: without a
// readable last message there is nothing to judge. See ADR-026.

import { readFileSync } from 'node:fs';

let input;
try {
  input = JSON.parse(readFileSync(0, 'utf8'));
} catch {
  process.exit(0);
}

const msg = input?.last_assistant_message ?? '';
if (typeof msg !== 'string' || msg === '') process.exit(0);

const completionClaims = /\b(done|complete|fixed|shipped|resolved|finished|all\s+set)\b/i;
const unfinishedLanguage = /\b(you can run|not yet tested|remaining|follow-up|future work|I'll leave|TODO|FIXME)\b/i;

if (completionClaims.test(msg) && unfinishedLanguage.test(msg)) {
  process.stdout.write(JSON.stringify({
    decision: 'block',
    reason: 'Completion claimed but unfinished-work language detected. Finish the work, file it, or close the task.',
  }));
}
process.exit(0);
