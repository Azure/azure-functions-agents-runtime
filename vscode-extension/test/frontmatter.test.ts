import { test } from "node:test";
import assert from "node:assert/strict";
import { locateFrontMatter, topLevelKeys, valueRangeForPath } from "../src/core/frontmatter";

const SAMPLE = `---
name: My Agent
description: A helpful assistant
builtin_endpoints: true
---

You are a helpful assistant.
`;

test("locateFrontMatter finds the block and parses YAML", () => {
  const fm = locateFrontMatter(SAMPLE);
  assert.ok(fm, "front matter should be located");
  const data = fm!.doc.toJS() as Record<string, unknown>;
  assert.equal(data.name, "My Agent");
  assert.equal(data.builtin_endpoints, true);
});

test("locateFrontMatter returns undefined without a leading fence", () => {
  assert.equal(locateFrontMatter("# just markdown\n"), undefined);
});

test("topLevelKeys lists authored keys", () => {
  const fm = locateFrontMatter(SAMPLE)!;
  assert.deepEqual(topLevelKeys(fm).sort(), ["builtin_endpoints", "description", "name"]);
});

test("valueRangeForPath maps to offsets inside the document", () => {
  const fm = locateFrontMatter(SAMPLE)!;
  const range = valueRangeForPath(fm, ["description"]);
  assert.ok(range, "range for description should resolve");
  const [start, end] = range!;
  assert.equal(SAMPLE.slice(start, end).trim(), "A helpful assistant");
});
