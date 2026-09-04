import { test } from "node:test";
import assert from "node:assert/strict";
import { slugFromFilename, safeFunctionName, isAgentFilename, isMainAlias } from "../src/core/slug";

test("slugFromFilename handles main aliases", () => {
  assert.equal(slugFromFilename("agent.md"), "main");
  assert.equal(slugFromFilename("CLAUDE.md"), "main");
  assert.equal(slugFromFilename("main.agent.md"), "main");
});

test("slugFromFilename sanitizes stems", () => {
  assert.equal(slugFromFilename("daily-report.agent.md"), "daily_report");
  assert.equal(slugFromFilename("Daily Azure Report.agent.md"), "Daily_Azure_Report");
  assert.equal(slugFromFilename("summarizer.claude.md"), "summarizer");
});

test("safeFunctionName prefixes leading digits and trims underscores", () => {
  assert.equal(safeFunctionName("9lives"), "fn_9lives");
  assert.equal(safeFunctionName("__weird__"), "weird");
  assert.equal(safeFunctionName("!!!"), "agent_function");
});

test("isAgentFilename / isMainAlias", () => {
  assert.equal(isAgentFilename("x.agent.md"), true);
  assert.equal(isAgentFilename("x.claude.md"), true);
  assert.equal(isAgentFilename("readme.md"), false);
  assert.equal(isMainAlias("main.agent.md"), true);
  assert.equal(isMainAlias("other.agent.md"), false);
});
