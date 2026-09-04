import { test } from "node:test";
import assert from "node:assert/strict";
import { addSubagent } from "../src/core/subagent";
import { summarizeAgent } from "../src/core/agentSummary";

const BASE = `---
name: Coordinator
description: Routes questions
builtin_endpoints: true
---

You are a coordinator.
`;

test("addSubagent creates a subagents block when absent", () => {
  const res = addSubagent(BASE, { agent: "billing", when: "Invoices and refunds" });
  assert.ok(res.ok, "expected success");
  const text = (res as { ok: true; text: string }).text;
  assert.match(text, /subagents:/);
  assert.match(text, /- agent: billing/);
  assert.match(text, /when: Invoices and refunds/);
  // Body and existing keys preserved.
  assert.match(text, /You are a coordinator\./);
  assert.match(text, /name: Coordinator/);
  const summary = summarizeAgent(text);
  assert.deepEqual(summary.subagents, ["billing"]);
});

test("addSubagent appends to an existing block and omits empty when", () => {
  const first = addSubagent(BASE, { agent: "billing", when: "Money" });
  assert.ok(first.ok);
  const second = addSubagent((first as { ok: true; text: string }).text, { agent: "tech" });
  assert.ok(second.ok);
  const summary = summarizeAgent((second as { ok: true; text: string }).text);
  assert.deepEqual(summary.subagents.sort(), ["billing", "tech"]);
});

test("addSubagent rejects duplicates and empty slugs", () => {
  const dup = addSubagent(
    `---\nname: A\ndescription: d\nsubagents:\n  - agent: billing\n---\nbody\n`,
    { agent: "billing" }
  );
  assert.equal(dup.ok, false);
  const empty = addSubagent(BASE, { agent: "  " });
  assert.equal(empty.ok, false);
});

test("addSubagent fails without front matter", () => {
  const res = addSubagent("# no front matter\n", { agent: "billing" });
  assert.equal(res.ok, false);
});

test("summarizeAgent reads trigger, endpoints, and chat UI", () => {
  const http = summarizeAgent(
    `---\nname: N\ndescription: d\ntrigger:\n  type: http\nbuiltin_endpoints:\n  debug_chat_ui: true\n---\nbody\n`
  );
  assert.equal(http.name, "N");
  assert.equal(http.triggerType, "http");
  assert.equal(http.endpointsEnabled, true);
  assert.equal(http.hasChatUI, true);

  const noUi = summarizeAgent(
    `---\nname: N\ndescription: d\nbuiltin_endpoints:\n  chat_api: true\n---\nbody\n`
  );
  assert.equal(noUi.endpointsEnabled, true);
  assert.equal(noUi.hasChatUI, false);
});
