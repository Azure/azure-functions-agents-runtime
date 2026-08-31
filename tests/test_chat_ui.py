from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

CHAT_UI_PATH = (
    Path(__file__).parents[1]
    / "src"
    / "azure_functions_agents"
    / "public"
    / "index.html"
)


def _script_text() -> str:
    html = CHAT_UI_PATH.read_text(encoding="utf-8")
    return html.rsplit("<script>", 1)[1].split("</script>", 1)[0]


def _css_rule(selector: str) -> str:
    html = CHAT_UI_PATH.read_text(encoding="utf-8")
    start = html.index(f"{selector} {{")
    end = html.index("}", start)
    return html[start:end]


def test_workflow_card_wraps_long_structured_status_inside_chat_width() -> None:
    card_rule = _css_rule(".workflow-card")
    status_rule = _css_rule(".workflow-card-custom-status")

    assert "min-width: 0;" in card_rule
    assert "overflow-wrap: anywhere;" in status_rule


def _history_replay_functions(script: str) -> str:
    start = script.index("function invalidateHistoryReplay()")
    end = script.index("\n\t\tfunction renderWaitingBubble()", start)
    return script[start:end]


def _run_node(harness: str) -> None:
    result = subprocess.run(
        [shutil.which("node") or "node", "--input-type=module", "-e", harness],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr or result.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_delayed_history_response_cannot_replace_newer_chat_activity() -> None:
    functions = _history_replay_functions(_script_text())
    harness = textwrap.dedent(
        """
        const state = {
          baseUrl: "https://example.test",
          sessionId: "session-a",
        };
        let historyReplayGeneration = 0;
        let historyReplayInFlight = false;
        let restoredGreetingContent = {};
        let resolveFetch;
        let startCalls = 0;
        let stopCalls = 0;
        const rendered = [];
        const statuses = [];
        const chatEl = { innerHTML: "initial" };

        function isValidSessionId(value) { return Boolean(value); }
        function getApiBasePath() { return "/agents/main"; }
        function buildAuthQuery() { return ""; }
        function hideDetails() {}
        function stopWorkflowPolling() { stopCalls += 1; }
        function startWorkflowPolling() { startCalls += 1; }
        function scrollChatToBottom() {}
        function shortSessionId(value) { return value; }
        function setStatus(value) { statuses.push(value); }
        function renderBubble(role, text, meta) {
          rendered.push({ role, text, meta });
          return {};
        }
        globalThis.fetch = () => new Promise((resolve) => { resolveFetch = resolve; });

        __HISTORY_FUNCTIONS__

        const staleReplay = loadSessionHistory("session-a", {
          announce: false,
          replaceView: false,
        });
        chatEl.innerHTML = "new turn";
        invalidateHistoryReplay();
        startWorkflowPolling();
        resolveFetch({
          ok: true,
          json: async () => ({
            messages: [{ role: "user", text: "old message" }],
            truncated: false,
          }),
        });
        await staleReplay;
        if (chatEl.innerHTML !== "new turn" || rendered.length !== 0) {
          throw new Error("stale history replaced newer chat activity");
        }

        chatEl.innerHTML = "before current replay";
        const currentReplay = loadSessionHistory("session-a", {
          announce: true,
          replaceView: false,
        });
        resolveFetch({
          ok: true,
          json: async () => ({
            messages: [{ role: "assistant", text: "latest message" }],
            truncated: true,
          }),
        });
        await currentReplay;
        if (chatEl.innerHTML !== "") {
          throw new Error("current history did not replace the transcript");
        }
        if (rendered.length !== 2 || rendered[0].meta !== "History") {
          throw new Error("truncated replay did not render its history notice");
        }
        if (!statuses.some((value) => value.includes("Older messages were omitted"))) {
          throw new Error("truncated replay did not announce omitted messages");
        }
        if (stopCalls !== 2 || startCalls !== 2 || historyReplayInFlight) {
          throw new Error("history replay did not coordinate workflow polling");
        }
        """
    ).replace("__HISTORY_FUNCTIONS__", functions)

    _run_node(harness)

@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_delayed_workflow_response_is_discarded_after_session_switch() -> None:
    script = _script_text()
    start = script.index("function workflowIdentityIsCurrent(")
    end = script.index("\n\t\tfunction startWorkflowPolling()", start)
    functions = script[start:end]
    harness = textwrap.dedent(
        """
        const state = {
          baseUrl: "https://example.test",
          sessionId: "session-a",
        };
        const document = { visibilityState: "visible" };
        const WORKFLOW_POLL_FAILURE_WARN_AT = 3;
        let workflowEpoch = 0;
        let workflowPollInFlight = false;
        let workflowPollFailureCount = 0;
        let workflowsCapability = "supported";
        let resolveFetch;
        const upserts = [];

        function getApiBasePath() { return "/agents/main"; }
        function buildAuthQuery() { return ""; }
        function markWorkflowsUnsupported() {}
        function upsertWorkflowCard(value) { upserts.push(value); }
        function stopWorkflowPolling() {}
        function resetWorkflowCards() {}
        globalThis.fetch = () => new Promise((resolve) => { resolveFetch = resolve; });

        __WORKFLOW_FUNCTIONS__

        const pending = pollWorkflowsOnce();
        state.sessionId = "session-b";
        resolveFetch({
          ok: true,
          status: 200,
          json: async () => ({ workflows: [{ id: "old-session-workflow" }] }),
        });
        await pending;
        if (upserts.length !== 0) {
          throw new Error("stale workflow response populated the new session");
        }
        if (workflowPollInFlight) {
          throw new Error("workflow poll did not leave single-flight state");
        }
        """
    ).replace("__WORKFLOW_FUNCTIONS__", functions)

    _run_node(harness)


def test_session_changes_invalidate_history_and_workflow_identity() -> None:
    script = _script_text()

    submit_start = script.index('composerEl.addEventListener("submit"')
    submit_end = script.index('promptInputEl.addEventListener("keydown"', submit_start)
    submit_block = script[submit_start:submit_end]
    assert "invalidateHistoryReplay();" in submit_block
    assert "startWorkflowPolling();" in submit_block

    resume_start = script.index(
        "if (sessionIdRaw && !sessionFieldUntouched && sessionIdRaw !== state.sessionId)"
    )
    resume_end = script.index("updateSessionBar();", resume_start)
    resume_block = script[resume_start:resume_end]
    assert "invalidateHistoryReplay();" in resume_block
    assert "resetWorkflowStateForSessionChange();" in resume_block

    new_session_start = script.index('newSessionBtnEl.addEventListener("click"')
    new_session_end = script.index('settingsBtnEl.addEventListener("click"', new_session_start)
    new_session_block = script[new_session_start:new_session_end]
    assert "invalidateHistoryReplay();" in new_session_block
    assert "resetWorkflowStateForSessionChange();" in new_session_block

    poll_start = script.index("async function pollWorkflowsOnce()")
    poll_end = script.index("function startWorkflowPolling()", poll_start)
    poll_block = script[poll_start:poll_end]
    assert "const epoch = workflowEpoch;" in poll_block
    assert poll_block.count("workflowIdentityIsCurrent(") == 3


def _status_formatter_functions(script: str) -> str:
    """Extract the two pure `custom_status` formatter helpers.

    ``formatWorkflowStatus`` and ``formatDynamicWorkflowStatus`` live
    contiguously just above ``renderWorkflowCard`` so they can be lifted
    into a Node harness without their DOM-touching caller.
    """
    start = script.index("function formatWorkflowStatus(")
    end = script.index("function renderWorkflowCard(", start)
    return script[start:end]


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_format_workflow_status_passes_legacy_string_through_verbatim() -> None:
    functions = _status_formatter_functions(_script_text())
    harness = textwrap.dedent(
        """
        __STATUS_FUNCTIONS__

        // Schema version 1: a legacy free-form string must render exactly
        // as authored, byte-for-byte, with no reformatting.
        const legacy = "3/7 tasks done, current=summarize";
        if (formatWorkflowStatus(legacy) !== legacy) {
          throw new Error("legacy string custom_status was not preserved verbatim");
        }
        // Non-string, non-object inputs collapse to an empty status line.
        for (const value of [null, undefined, 42]) {
          if (formatWorkflowStatus(value) !== "") {
            throw new Error("non-string/non-object custom_status did not degrade to empty");
          }
        }
        // An unknown object shape (future schema_version) degrades to JSON,
        // never "[object Object]", and never throws.
        const unknown = { schema_version: 99, foo: "bar" };
        const rendered = formatWorkflowStatus(unknown);
        if (rendered.includes("[object Object]")) {
          throw new Error("unknown object shape stringified to [object Object]");
        }
        if (rendered !== JSON.stringify(unknown)) {
          throw new Error("unknown object shape did not degrade to JSON");
        }
        // A cyclic object can't be JSON-serialized; the formatter must still
        // return a string rather than throwing.
        const cyclic = { schema_version: 5 };
        cyclic.self = cyclic;
        if (typeof formatWorkflowStatus(cyclic) !== "string") {
          throw new Error("unserializable object shape threw instead of degrading");
        }
        """
    ).replace("__STATUS_FUNCTIONS__", functions)

    _run_node(harness)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_format_workflow_status_renders_v2_dynamic_snapshot() -> None:
    functions = _status_formatter_functions(_script_text())
    harness = textwrap.dedent(
        """
        __STATUS_FUNCTIONS__

        // A running for_each expansion: expanded logical node with completed,
        // skipped, and running instances alongside static nodes.
        const running = {
          schema_version: 2,
          counts: {
            logical_total: 3,
            materialized_total: 5,
            completed: 2,
            skipped: 1,
            running: 1,
          },
          nodes: {
            discover: { state: "completed" },
            analyze: {
              state: "running",
              expanded_count: 3,
              instances: {
                "analyze[0]": { state: "completed" },
                "analyze[1]": { state: "skipped" },
                "analyze[2]": { state: "running" },
              },
            },
            summarize: { state: "pending" },
          },
        };
        const runningLine = formatWorkflowStatus(running);
        const expectedRunning =
          "2/5 done \u00b7 1 running \u00b7 1 skipped — " +
          "discover: completed, analyze: running (1/3), summarize: pending";
        if (runningLine !== expectedRunning) {
          throw new Error("running v2 snapshot rendered as: " + runningLine);
        }

        // An aggregated for_each node after its ordered result committed.
        const aggregated = {
          schema_version: 2,
          counts: {
            logical_total: 2,
            materialized_total: 2,
            completed: 1,
            skipped: 1,
            running: 0,
          },
          nodes: {
            analyze: {
              state: "aggregated",
              expanded_count: 2,
              instances: {
                "analyze[0]": { state: "completed" },
                "analyze[1]": { state: "skipped" },
              },
            },
            summarize: { state: "completed" },
          },
        };
        const aggregatedLine = formatWorkflowStatus(aggregated);
        if (aggregatedLine !==
          "1/2 done \u00b7 1 skipped — analyze: aggregated (1/2), summarize: completed") {
          throw new Error("aggregated v2 snapshot rendered as: " + aggregatedLine);
        }

        // A freshly-expanded node (materialized, nothing running yet) must
        // surface the `expanded` state and a 0/N instance progress.
        const expanded = {
          schema_version: 2,
          counts: {
            logical_total: 2,
            materialized_total: 3,
            completed: 0,
            skipped: 0,
            running: 0,
          },
          nodes: {
            analyze: {
              state: "expanded",
              expanded_count: 2,
              instances: {
                "analyze[0]": { state: "pending" },
                "analyze[1]": { state: "pending" },
              },
            },
          },
        };
        const expandedLine = formatWorkflowStatus(expanded);
        if (!expandedLine.includes("analyze: expanded (0/2)")) {
          throw new Error("expanded v2 snapshot missing instance progress: " + expandedLine);
        }
        if (!expandedLine.startsWith("0/3 done")) {
          throw new Error("expanded v2 snapshot miscounted: " + expandedLine);
        }

        // Malformed / partial v2 objects must degrade, not throw.
        for (const partial of [
          { schema_version: 2 },
          { schema_version: 2, counts: null, nodes: "oops" },
          { schema_version: 2, counts: {}, nodes: { a: null } },
        ]) {
          const out = formatWorkflowStatus(partial);
          if (typeof out !== "string") {
            throw new Error("partial v2 snapshot did not degrade to a string");
          }
          if (out.includes("[object Object]")) {
            throw new Error("partial v2 snapshot leaked [object Object]");
          }
        }
        """
    ).replace("__STATUS_FUNCTIONS__", functions)

    _run_node(harness)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_format_workflow_status_renders_v3_execution_state_safely() -> None:
    functions = _status_formatter_functions(_script_text())
    harness = textwrap.dedent(
        """
        __STATUS_FUNCTIONS__
        const status = {
          schema_version: 3,
          counts: {
            logical_total: 2, materialized_total: 2, pending: 0,
            running: 0, retry_wait: 1, completed: 0, skipped: 0,
            failed_continued: 1, failed: 1
          },
          nodes: {
            retry: {
              state: "retry_wait", attempt: 2, max_attempts: 3,
              next_retry_time: "2026-08-24T01:00:00+00:00"
            },
            fan: {
              state: "aggregated_with_errors", expanded_count: 1,
              instances: {
                "fan[0]": { state: "failed_continued", attempt: 1, max_attempts: 1 }
              }
            }
          }
        };
        const rendered = formatWorkflowStatus(status);
        for (const expected of [
          "1 retrying", "1 continued failure", "1 failed", "retry: retry_wait (2/3)",
          "fan: aggregated_with_errors", "fan[0] failed_continued 1/1"
        ]) {
          if (!rendered.includes(expected)) throw new Error("missing " + expected + ": " + rendered);
        }
        if (rendered.includes("idempotency") || rendered.includes("<script>")) {
          throw new Error("v3 formatter exposed hidden or injectable content");
        }
        """
    ).replace("__STATUS_FUNCTIONS__", functions)
    _run_node(harness)
