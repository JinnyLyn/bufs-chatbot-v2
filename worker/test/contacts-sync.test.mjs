// The outage page carries a copy of the sidebar's contact directory (the Worker has no build
// step and cannot import the app). This test fails when the two copies drift — as soon as
// frontend/src/lib/constants.ts (PR #289) carries CONTACT_GROUPS; before that it is skipped.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { CONTACTS } from "../src/pages.js";

const here = dirname(fileURLToPath(import.meta.url));
const constantsPath = join(here, "..", "..", "frontend", "src", "lib", "constants.ts");

function frontendExtensions() {
  if (!existsSync(constantsPath)) return null;
  const src = readFileSync(constantsPath, "utf8");
  const start = src.indexOf("export const CONTACT_GROUPS");
  if (start === -1) return null;
  const block = src.slice(start, src.indexOf("];", start));
  return [...block.matchAll(/ext:\s*"(\d{4})"/g)].map((m) => m[1]).sort();
}

test("outage page extensions match the frontend sidebar (CONTACT_GROUPS)", (t) => {
  const expected = frontendExtensions();
  if (!expected) {
    t.skip("frontend CONTACT_GROUPS not present on this checkout (lands with PR #289)");
    return;
  }
  const actual = CONTACTS.flatMap((c) => c.exts ?? c.items.map(([, n]) => n)).sort();
  assert.deepEqual(actual, expected);
});
