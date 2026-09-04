import { test } from "node:test";
import assert from "node:assert/strict";
import { toSkillSlug, buildSkillMarkdown } from "../src/core/skillTemplate";

test("toSkillSlug produces kebab-case folder names", () => {
  assert.equal(toSkillSlug("Azure Resources"), "azure-resources");
  assert.equal(toSkillSlug("PR Review Guide"), "pr-review-guide");
  assert.equal(toSkillSlug("  weird__name!!"), "weird-name");
  assert.equal(toSkillSlug(""), "my-skill");
});

test("buildSkillMarkdown emits name/description front matter and a title", () => {
  const md = buildSkillMarkdown({ name: "Azure Resources", description: "Query ARM." });
  assert.match(md, /^---\n/);
  assert.match(md, /^name: azure-resources$/m);
  assert.match(md, /^description: Query ARM\.$/m);
  assert.match(md, /^# Azure Resources$/m);
});
