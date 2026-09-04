import { test } from "node:test";
import assert from "node:assert/strict";
import {
  toPythonIdentifier,
  toolFileName,
  buildToolPython,
} from "../src/core/toolTemplate";

test("toPythonIdentifier produces snake_case identifiers", () => {
  assert.equal(toPythonIdentifier("Get Weather"), "get_weather");
  assert.equal(toPythonIdentifier("fetchPRStatus"), "fetch_pr_status");
  assert.equal(toPythonIdentifier("  spaced  name  "), "spaced_name");
});

test("toPythonIdentifier avoids leading digits, underscores, and keywords", () => {
  assert.equal(toPythonIdentifier("3things"), "tool_3things");
  assert.equal(toPythonIdentifier("__private__"), "private");
  assert.equal(toPythonIdentifier("class"), "class_tool");
  assert.equal(toPythonIdentifier("!!!"), "my_tool");
});

test("toolFileName never starts with underscore", () => {
  assert.equal(toolFileName("Get Weather"), "get_weather.py");
  assert.equal(toolFileName("_hidden"), "hidden.py");
});

test("buildToolPython emits a discoverable plain function with a docstring", () => {
  const src = buildToolPython({ name: "Get Weather", description: "Return the weather." });
  assert.match(src, /^def get_weather\(query: str\) -> dict:/m);
  assert.match(src, /"""Return the weather\./);
  assert.doesNotMatch(src, /^def _/m);
});

test("buildToolPython can emit async functions", () => {
  const src = buildToolPython({ name: "fetch", description: "Fetch data.", async: true });
  assert.match(src, /^async def fetch\(/m);
});
