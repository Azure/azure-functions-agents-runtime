import { test } from "node:test";
import assert from "node:assert/strict";
import { parse } from "yaml";
import { buildAgentMarkdown, coerceScalar } from "../src/core/agentTemplate";
import { locateFrontMatter } from "../src/core/frontmatter";

test("coerceScalar types values", () => {
  assert.equal(coerceScalar("true"), true);
  assert.equal(coerceScalar("42"), 42);
  assert.deepEqual(coerceScalar('["POST"]'), ["POST"]);
  assert.equal(coerceScalar("my-queue"), "my-queue");
});

test("buildAgentMarkdown produces parseable front matter for a timer agent", () => {
  const md = buildAgentMarkdown({
    name: "Daily Report",
    description: "Sends a daily report.",
    triggerType: "timer_trigger",
    triggerArgs: { schedule: "0 0 9 * * *" },
  });
  const fm = locateFrontMatter(md);
  assert.ok(fm, "generated file has front matter");
  const data = parse(fm!.yamlText) as Record<string, any>;
  assert.equal(data.name, "Daily Report");
  assert.equal(data.trigger.type, "timer_trigger");
  assert.equal(data.trigger.args.schedule, "0 0 9 * * *");
  assert.equal(data.builtin_endpoints, undefined);
});

test("buildAgentMarkdown enables endpoints when requested", () => {
  const md = buildAgentMarkdown({
    name: "Chat",
    description: "Interactive.",
    builtinEndpoints: true,
  });
  const data = parse(locateFrontMatter(md)!.yamlText) as Record<string, any>;
  assert.equal(data.builtin_endpoints, true);
  assert.equal(data.trigger, undefined);
});
