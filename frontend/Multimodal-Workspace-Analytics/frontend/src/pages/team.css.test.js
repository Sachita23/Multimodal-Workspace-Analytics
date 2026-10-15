// team.css is a lazy chunk that Vite appends after index.css the first time
// a Team page opens, and it stays for the rest of the visit. Any rule in it
// that restyles a global class therefore also restyles the single-user
// pages (it once turned the amber "Collector stopped" pill white).
// Run with `node --test src/pages/team.css.test.js`.

import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";


const PAGES = dirname(fileURLToPath(import.meta.url));
const SRC = join(PAGES, "..");

const TEAM_SOURCES = new Set([
  "pages/Team.jsx",
  "pages/TeamMember.jsx",
  "components/AdminKeyForm.jsx",
]);


// --------------------------------------------------
// A small CSS reader: enough for these two hand-written files
// --------------------------------------------------

function skipBlock(css, open) {
  let depth = 0;

  for (let i = open; i < css.length; i++) {
    if (css[i] === "{") depth += 1;
    if (css[i] === "}") depth -= 1;
    if (depth === 0) return i + 1;
  }

  throw new Error("Unbalanced braces");
}


function splitSelectors(prelude) {
  const parts = [];
  let depth = 0;
  let start = 0;

  for (let i = 0; i < prelude.length; i++) {
    if (prelude[i] === "(") depth += 1;
    if (prelude[i] === ")") depth -= 1;

    if (prelude[i] === "," && depth === 0) {
      parts.push(prelude.slice(start, i));
      start = i + 1;
    }
  }

  parts.push(prelude.slice(start));
  return parts.map((part) => part.trim().replace(/\s+/g, " ")).filter(Boolean);
}


function parseDeclarations(body) {
  const declarations = new Map();

  for (const part of body.split(";")) {
    const colon = part.indexOf(":");

    if (colon > 0) {
      declarations.set(
        part.slice(0, colon).trim().toLowerCase(),
        part.slice(colon + 1).trim().replace(/\s+/g, " ")
      );
    }
  }

  return declarations;
}


/** { rules: [{ media, selector, declarations }], keyframes: Set } */
function parseCss(text, media = "", out = { rules: [], keyframes: new Set() }) {
  const css = text.replace(/\/\*[\s\S]*?\*\//g, "");
  let i = 0;

  while (i < css.length) {
    const open = css.indexOf("{", i);

    if (open === -1) break;

    const prelude = css.slice(i, open).trim();
    const end = skipBlock(css, open);
    const body = css.slice(open + 1, end - 1);

    if (prelude.startsWith("@media")) {
      parseCss(body, prelude.replace(/\s+/g, " "), out);
    } else if (prelude.startsWith("@keyframes")) {
      out.keyframes.add(prelude.split(/\s+/)[1]);
    } else if (!prelude.startsWith("@")) {
      for (const selector of splitSelectors(prelude)) {
        out.rules.push({ media, selector, declarations: parseDeclarations(body) });
      }
    }

    i = end;
  }

  return out;
}


// Classes the selector's subject (its last compound) requires.
function subjectClasses(selector) {
  const compounds = selector.split(/\s*[\s>+~]\s*/).filter(Boolean);
  const subject = compounds[compounds.length - 1].replace(/::?[\w-]+(\([^)]*\))?/g, "");
  return [...subject.matchAll(/\.([\w-]+)/g)].map((match) => match[1]);
}


// Class names rendered by the single-user (non-team) components.
function singleUserClasses() {
  const classes = new Set();

  function walk(dir) {
    for (const name of readdirSync(dir)) {
      const path = join(dir, name);

      if (statSync(path).isDirectory()) {
        walk(path);
        continue;
      }

      if (!/\.(jsx|js)$/.test(name) || name.includes(".test.")) continue;
      if (TEAM_SOURCES.has(relative(SRC, path))) continue;

      const source = readFileSync(path, "utf8");

      // className="..." or className={...} with one level of nested braces
      // (template literals such as `live-indicator live-${tone}`).
      for (const match of source.matchAll(/className=(\{(?:[^{}]|\{[^{}]*\})*\}|"[^"]*")/g)) {
        for (const literal of match[1].matchAll(/"([^"]*)"|'([^']*)'|`([^`]*)`/g)) {
          const text = (literal[1] ?? literal[2] ?? literal[3]).replace(/\$\{[^}]*\}/g, " ");
          text.split(/\s+/).filter((token) => /^[a-zA-Z][\w-]*$/.test(token)).forEach((token) => classes.add(token));
        }
      }
    }
  }

  walk(SRC);
  return classes;
}


const team = parseCss(readFileSync(join(PAGES, "team.css"), "utf8"));
const global = parseCss(readFileSync(join(SRC, "index.css"), "utf8"));

const globalRules = new Map();

for (const rule of global.rules) {
  const key = `${rule.media}|${rule.selector}`;
  const merged = globalRules.get(key) ?? new Map();
  rule.declarations.forEach((value, prop) => merged.set(prop, value));
  globalRules.set(key, merged);
}


// --------------------------------------------------
// Tests
// --------------------------------------------------

test("the CSS reader finds the rules it needs to check", () => {
  assert.ok(team.rules.length > 50);
  assert.ok(global.rules.length > 100);
  assert.ok(global.keyframes.has("live-pulse"));
  assert.ok(globalRules.has("|.live-indicator"));
  assert.deepEqual(subjectClasses(".team-actions .team-live-toggle:hover"), ["team-live-toggle"]);
  assert.deepEqual(subjectClasses(".team-live-dot.is-live"), ["team-live-dot", "is-live"]);
  assert.ok(singleUserClasses().has("live-indicator"), "LiveIndicator.jsx's class is seen");
});


test("team.css does not redefine global @keyframes", () => {
  const clashes = [...team.keyframes].filter((name) => global.keyframes.has(name));
  assert.deepEqual(clashes, []);
});


test("rules team.css shares with index.css only mirror it (same values)", () => {
  const conflicts = [];

  for (const rule of team.rules) {
    const globalDeclarations = globalRules.get(`${rule.media}|${rule.selector}`);

    if (!globalDeclarations) continue;

    for (const [prop, value] of rule.declarations) {
      if (globalDeclarations.has(prop) && globalDeclarations.get(prop) !== value) {
        conflicts.push(`${rule.media} ${rule.selector} { ${prop}: ${value} } vs index.css ${globalDeclarations.get(prop)}`.trim());
      }
    }
  }

  assert.deepEqual(conflicts, []);
});


test("team.css does not style elements of the single-user pages", () => {
  const used = singleUserClasses();
  const offenders = [];

  for (const rule of team.rules) {
    // An exact mirror of an index.css rule is checked by the test above.
    if (globalRules.has(`${rule.media}|${rule.selector}`)) continue;

    const classes = subjectClasses(rule.selector);

    if (classes.length > 0 && classes.every((name) => used.has(name))) {
      offenders.push(`${rule.media} ${rule.selector}`.trim());
    }
  }

  assert.deepEqual(offenders, []);
});
