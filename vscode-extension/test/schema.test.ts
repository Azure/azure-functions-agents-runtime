import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { createValidator, validateData, formatSchemaError } from "../src/core/schemaValidate";

function loadSchema(name: string): object {
  const p = join(__dirname, "..", "..", "schemas", name);
  return JSON.parse(readFileSync(p, "utf-8"));
}

test("generated agent schema compiles and accepts a valid agent", () => {
  const validate = createValidator(loadSchema("agent.schema.json"));
  const result = validateData(validate, {
    name: "My Agent",
    description: "A helpful assistant",
    builtin_endpoints: true,
  });
  assert.equal(result.valid, true, JSON.stringify(result.errors));
});

test("agent schema rejects unknown property (additionalProperties: false)", () => {
  const validate = createValidator(loadSchema("agent.schema.json"));
  const result = validateData(validate, {
    name: "X",
    description: "Y",
    bogus_field: 1,
  });
  assert.equal(result.valid, false);
  assert.ok(result.errors.some((e) => e.keyword === "additionalProperties"));
  assert.ok(result.errors.map(formatSchemaError).join(" ").includes("bogus_field"));
});

test("agent schema requires name and description", () => {
  const validate = createValidator(loadSchema("agent.schema.json"));
  const result = validateData(validate, { name: "only-name" });
  assert.equal(result.valid, false);
  assert.ok(result.errors.some((e) => e.keyword === "required"));
});

test("global config schema compiles and accepts defaults", () => {
  const validate = createValidator(loadSchema("agents-config.schema.json"));
  const result = validateData(validate, { model: "$FOUNDRY_MODEL", timeout: 900 });
  assert.equal(result.valid, true, JSON.stringify(result.errors));
});
