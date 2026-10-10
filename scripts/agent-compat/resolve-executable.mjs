// Resolves a client program name to one absolute, real path before anything sensitive happens.
//
// The credential and review launchers used to pass a bare name such as `pass-cli` to the
// operating system, which searched the caller's PATH. That search also reaches empty, relative
// and `.` entries, and any directory inside the working checkout. A launcher resolves the name
// here first, so a file planted in one of those places is never run, and then spawns the
// absolute path it got back.
//
// The rule: search only PATH entries that are absolute; skip an entry that is the working
// directory or inside it; take the first regular file with the execute bit; return its real path
// (symlinks followed), and refuse that too if it lands inside the working directory. When the
// working directory contains the user's home directory, a subtree rule would reject every tool
// under `~/.local/bin`, so only the working directory itself is refused in that case.
//
// Nothing here knows where any tool is installed. Homebrew, `~/.local/bin`, bun and npm globals
// all work as long as they are on an absolute PATH entry.

import { accessSync, constants, realpathSync, statSync } from 'node:fs';
import { homedir } from 'node:os';
import { delimiter, isAbsolute, join, relative, sep } from 'node:path';

function realOrSelf(path) {
  try {
    return realpathSync(path);
  } catch {
    return path;
  }
}

function isInside(parent, child) {
  const path = relative(parent, child);
  if (path === '') return true;
  return path !== '..' && !path.startsWith(`..${sep}`) && !isAbsolute(path);
}

function notFound(name) {
  const error = new Error(
    `${name} executable not found on an absolute search path outside the working directory`,
  );
  error.code = 'ENOENT';
  return error;
}

// The same rule applied to a whole search path, for a child process that will do its own
// lookups. The Context7 server starts with `#!/usr/bin/env node`, so the PATH it inherits
// decides which `node` receives the key.
export function trustedSearchPath(pathValue, {
  cwd = process.cwd(),
  home = homedir(),
} = {}) {
  const workingDirectory = realOrSelf(cwd);
  const subtreeRefused = !isInside(workingDirectory, realOrSelf(home));
  return String(pathValue ?? '')
    .split(delimiter)
    .filter((entry) => {
      if (!entry || !isAbsolute(entry)) return false;
      const directory = realOrSelf(entry);
      return !(directory === workingDirectory
        || (subtreeRefused && isInside(workingDirectory, directory)));
    })
    .join(delimiter);
}

export function resolveExecutable(name, {
  pathValue = process.env.PATH ?? '',
  cwd = process.cwd(),
  home = homedir(),
} = {}) {
  if (typeof name !== 'string' || name === '' || name.includes('/') || name.includes('\\')) {
    throw new Error('executable name must be a bare program name');
  }
  const workingDirectory = realOrSelf(cwd);
  const subtreeRefused = !isInside(workingDirectory, realOrSelf(home));
  const refused = (path) =>
    path === workingDirectory || (subtreeRefused && isInside(workingDirectory, path));

  for (const entry of pathValue.split(delimiter)) {
    if (!entry || !isAbsolute(entry)) continue;
    const directory = realOrSelf(entry);
    if (refused(directory)) continue;
    const candidate = join(directory, name);
    try {
      if (!statSync(candidate).isFile()) continue;
      accessSync(candidate, constants.X_OK);
      const real = realpathSync(candidate);
      if (refused(real)) continue;
      return real;
    } catch {
      // This entry does not hold a usable program. Try the next one.
    }
  }
  throw notFound(name);
}
