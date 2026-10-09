// Splits one command line into words exactly as /bin/sh would, or returns null.
//
// It carries no allowlist and no knowledge of any program. Each reader decides what the words
// mean. Anything a shell would expand, chain, redirect or split differently returns null, so a
// command this function accepts means the same text to a real shell.
//
// Words are separated by space and tab. Single-quoted text is literal. Double-quoted text is
// literal unless it holds `$`, a backtick, a backslash or `!`. Quoted and unquoted text that touch
// join into one word, and '' or "" make an empty word. The function never throws.

const ALWAYS_REFUSED = /[\0\n\r]/u;
const UNQUOTED_REFUSED = /[;&|<>`$\\(){}*?[\]]/u;
const WORD_START_REFUSED = /[~#!]/u;
const DOUBLE_QUOTED_REFUSED = /[$`\\!]/u;
const SEPARATOR = /[ \t]/u;
const OTHER_SPACE = /\s/u;

export function shellWords(command) {
  if (typeof command !== 'string' || ALWAYS_REFUSED.test(command)) return null;
  const words = [];
  let word = '';
  let inWord = false;
  let quote = null;
  for (const character of command) {
    if (quote === "'") {
      if (character === "'") quote = null;
      else word += character;
    } else if (quote === '"') {
      if (character === '"') quote = null;
      else if (DOUBLE_QUOTED_REFUSED.test(character)) return null;
      else word += character;
    } else if (character === "'" || character === '"') {
      quote = character;
      inWord = true;
    } else if (SEPARATOR.test(character)) {
      if (inWord) words.push(word);
      word = '';
      inWord = false;
    } else if (OTHER_SPACE.test(character) || UNQUOTED_REFUSED.test(character)) {
      return null;
    } else if (!inWord && WORD_START_REFUSED.test(character)) {
      return null;
    } else {
      word += character;
      inWord = true;
    }
  }
  if (quote !== null) return null;
  if (inWord) words.push(word);
  return words.length > 0 ? words : null;
}
