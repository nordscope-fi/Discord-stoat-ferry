// Installs the Context7 helper from the committed lockfile (#995).
//
// `npm ci` installs exactly what package-lock.json lists and fails when a downloaded tarball does
// not match the integrity hash recorded there. The lock covers the package and every package it
// depends on, so the whole tree is checked, which `npx` cannot do. The tree goes into a private
// folder beside the reviewer-runtime releases, named by the hash of the lock, and the launcher
// (context7-mcp.mjs) starts the server from there.
//
// This runs from the checkout during installReviewerRuntime and is not copied into the runtime.
//
// Registry isolation is a separate issue (#961). The two flags below only stop a configuration
// file from redirecting this one install: the registry is named on the command line, and the
// user config is an empty file.

import { spawnSync } from 'node:child_process';
import {
  chmodSync,
  mkdirSync,
  mkdtempSync,
  renameSync,
  rmSync,
  writeFileSync,
} from 'node:fs';
import { extname, join } from 'node:path';
import { context7Entrypoint, context7InstallDirectory } from './context7-mcp.mjs';
import { resolveExecutable, trustedSearchPath } from './resolve-executable.mjs';

const PUBLIC_REGISTRY = 'https://registry.npmjs.org/';
const PASSTHROUGH_ENVIRONMENT = [
  'PATH',
  'HOME',
  'TMPDIR',
  'LANG',
  'LC_ALL',
  'NODE_EXTRA_CA_CERTS',
  'HTTP_PROXY',
  'HTTPS_PROXY',
  'NO_PROXY',
  'http_proxy',
  'https_proxy',
  'no_proxy',
];
const INSTALL_TIMEOUT_MS = 10 * 60 * 1000;

function bytesOf(value) {
  return Buffer.isBuffer(value) ? value : Buffer.from(value);
}

function installEnvironment(source) {
  const environment = {};
  for (const name of PASSTHROUGH_ENVIRONMENT) {
    if (typeof source[name] === 'string') environment[name] = source[name];
  }
  environment.PATH = trustedSearchPath(environment.PATH ?? '');
  return environment;
}

function failure(detail) {
  return new Error(`Context7 package install failed: ${detail}`);
}

export function installContext7Packages({
  runtimeRoot,
  files,
  environment = process.env,
  resolve = resolveExecutable,
  run = spawnSync,
}) {
  const manifest = files?.['context7/package.json'];
  const lock = files?.['context7/package-lock.json'];
  if (!runtimeRoot || manifest === undefined || lock === undefined) {
    throw failure('the package manifest and lock are missing from the runtime files');
  }
  const directory = context7InstallDirectory(runtimeRoot, bytesOf(lock));
  try {
    return { installed: false, directory, entry: context7Entrypoint(directory) };
  } catch {
    // Missing, partial or for another version: install it again below.
  }

  const parent = join(runtimeRoot, 'context7');
  mkdirSync(parent, { recursive: true, mode: 0o700 });
  chmodSync(parent, 0o700);
  const staged = mkdtempSync(join(parent, '.install-'));
  try {
    chmodSync(staged, 0o700);
    writeFileSync(join(staged, 'package.json'), bytesOf(manifest), { mode: 0o600 });
    writeFileSync(join(staged, 'package-lock.json'), bytesOf(lock), { mode: 0o600 });
    const emptyConfig = join(staged, 'empty-config');
    writeFileSync(emptyConfig, '', { mode: 0o600 });

    const env = installEnvironment(environment);
    let npm;
    try {
      npm = resolve('npm', { pathValue: env.PATH });
    } catch {
      throw failure('npm executable not found');
    }
    // npm is usually a node script whose first line searches PATH for node. Run such a script
    // with the node that is running this installer.
    const [command, commandArgs] = ['.js', '.cjs', '.mjs'].includes(extname(npm))
      ? [process.execPath, [npm]]
      : [npm, []];
    const result = run(command, [
      ...commandArgs,
      'ci',
      '--ignore-scripts',
      '--no-audit',
      '--no-fund',
      '--registry',
      PUBLIC_REGISTRY,
      '--userconfig',
      emptyConfig,
    ], {
      cwd: staged,
      env,
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'pipe'],
      maxBuffer: 8 * 1024 * 1024,
      timeout: INSTALL_TIMEOUT_MS,
    });
    if (result.error || result.status !== 0) {
      const tail = String(result.stderr ?? result.error?.message ?? '').trim().slice(-800);
      throw failure(`npm ci exited with ${result.status ?? 'no status'}\n${tail}`);
    }
    rmSync(emptyConfig, { force: true });
    // npm ci can only install what the lock lists. Check the result is the reviewed package
    // before it is put where the launcher looks.
    try {
      context7Entrypoint(staged);
    } catch (error) {
      throw failure(error.message);
    }
    rmSync(directory, { recursive: true, force: true });
    renameSync(staged, directory);
    return { installed: true, directory, entry: context7Entrypoint(directory) };
  } catch (error) {
    rmSync(staged, { recursive: true, force: true });
    throw error;
  }
}
