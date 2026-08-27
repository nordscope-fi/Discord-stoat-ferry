#!/usr/bin/env node

import { execFileSync, spawnSync } from 'node:child_process';
import { realpathSync } from 'node:fs';
import { dirname } from 'node:path';
import { pathToFileURL } from 'node:url';

export const EXPECTED_PLAIN_ENGLISH_VERSION = '1.0.0';
export const PLAIN_ENGLISH_RECOVERY = 'npm install -g plain-english@1.0.0';
export const CODEX_CHAT_COMMAND =
  'node "$(git rev-parse --show-toplevel)/.codex/hooks/plain-english.mjs" ' +
  'hook chat --agent codex';

const VERSION_SHAPE = /^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$/u;

export function probePlainEnglish({ run = spawnSync } = {}) {
  const result = run('plain-english', ['--version'], {
    encoding: 'utf8',
    stdio: 'pipe',
  });
  const detected = typeof result.stdout === 'string' ? result.stdout.trim() : '';
  if (result.error?.code === 'ENOENT') {
    return { status: 'missing', expected: EXPECTED_PLAIN_ENGLISH_VERSION, detected: null };
  }
  if (result.status !== 0 || !VERSION_SHAPE.test(detected)) {
    return { status: 'malformed', expected: EXPECTED_PLAIN_ENGLISH_VERSION, detected };
  }
  return {
    status: detected === EXPECTED_PLAIN_ENGLISH_VERSION ? 'accepted' : 'mismatched',
    expected: EXPECTED_PLAIN_ENGLISH_VERSION,
    detected,
  };
}

export function plainEnglishFailure(result) {
  const detected = result.detected ? `; detected ${result.detected}` : '; no version detected';
  return `plain-English ${result.status}: expected ${result.expected}${detected}. ` +
    `Run ${PLAIN_ENGLISH_RECOVERY}`;
}

export function requirePlainEnglish(options = {}) {
  const result = probePlainEnglish(options);
  if (result.status !== 'accepted') throw new Error(plainEnglishFailure(result));
  return result;
}

export function canonicalCheckoutRoot(root) {
  const common = execFileSync(
    'git', ['rev-parse', '--path-format=absolute', '--git-common-dir'],
    { cwd: root, encoding: 'utf8' },
  ).trim();
  return dirname(realpathSync(common));
}

export function removeUnusedIssueChannel(document) {
  document.hooks = document.hooks ?? {};
  delete document.hooks.Issue;
  return document;
}

export function stripVibeIssueChannel(content) {
  const blocks = content.split(/(?=\[\[hooks\]\])/u);
  return blocks.filter((block) =>
    !block.includes('plain-english-issue') && !block.includes('_save_(issue|comment)')
  ).join('').replace(/\n{3,}/gu, '\n\n');
}

export function normalizeCodexChatHooks(document) {
  for (const event of ['Stop', 'SubagentStop']) {
    const matches = (document.hooks?.[event] ?? [])
      .flatMap((group) => group.hooks ?? [])
      .filter((hook) => hook.command === CODEX_CHAT_COMMAND);
    if (matches.length !== 1) {
      throw new Error(
        `expected one native plain-English chat hook for ${event}; found ${matches.length}`,
      );
    }
    if (![10, 60].includes(matches[0].timeout)) {
      throw new Error(
        `unexpected plain-English chat timeout for ${event}: ${matches[0].timeout}`,
      );
    }
    matches[0].timeout = 60;
  }
  return document;
}

function main() {
  if (process.argv[2] !== '--check') throw new Error('use --check');
  requirePlainEnglish();
}

if (process.argv[1] && pathToFileURL(process.argv[1]).href === import.meta.url) {
  try {
    main();
  } catch (error) {
    process.stderr.write(`${error.message}\n`);
    process.exitCode = 1;
  }
}
