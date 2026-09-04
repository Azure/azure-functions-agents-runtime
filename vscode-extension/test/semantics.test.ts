import { test } from "node:test";
import assert from "node:assert/strict";
import { checkAgentSemantics } from "../src/core/semantics";

test("warns when no trigger and no endpoints", () => {
  const issues = checkAgentSemantics({ name: "A", description: "d" });
  assert.ok(issues.some((i) => i.message.includes("no `trigger`")));
});

test("no trigger warning when endpoints enabled", () => {
  const issues = checkAgentSemantics({ name: "A", description: "d", builtin_endpoints: true });
  assert.equal(issues.some((i) => i.message.includes("no `trigger`")), false);
});

test("flags missing required timer schedule", () => {
  const issues = checkAgentSemantics({ name: "A", description: "d", trigger: { type: "timer_trigger" } });
  assert.ok(issues.some((i) => i.message.includes('requires arg "schedule"')));
});

test("flags implausible cron", () => {
  const issues = checkAgentSemantics({
    name: "A",
    description: "d",
    trigger: { type: "timer_trigger", args: { schedule: "not a cron" } },
  });
  assert.ok(issues.some((i) => i.message.includes("NCRONTAB")));
});

test("accepts a valid 6-field cron", () => {
  const issues = checkAgentSemantics({
    name: "A",
    description: "d",
    trigger: { type: "timer_trigger", args: { schedule: "0 0 9 * * *" } },
  });
  assert.equal(issues.length, 0);
});

test("flags unsupported trigger type with guidance", () => {
  const issues = checkAgentSemantics({ name: "A", description: "d", trigger: { type: "schedule" } });
  assert.ok(issues.some((i) => i.message.includes("Use `timer_trigger`")));
});

test("flags unknown trigger type", () => {
  const issues = checkAgentSemantics({ name: "A", description: "d", trigger: { type: "banana_trigger" } });
  assert.ok(issues.some((i) => i.message.includes("Unknown trigger type")));
});
