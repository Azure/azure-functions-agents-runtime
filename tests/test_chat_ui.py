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


def _history_replay_functions(script: str) -> str:
    start = script.index("function invalidateHistoryReplay()")
    end = script.index("\n\t\tfunction renderWaitingBubble()", start)
    return script[start:end]


def _send_message_function(script: str) -> str:
    start = script.index("async function sendMessage(")
    end = script.index('\n\t\tcomposerEl.addEventListener("submit"', start)
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


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_trace_run_detail_renders_copies_and_resets() -> None:
    script = _script_text()
    trace_validation_start = script.index("function isTraceId(")
    trace_validation_end = script.index("\n\t\tfunction buildAuthQuery()", trace_validation_start)
    trace_details_start = script.index("function updateRunDetailsBar()")
    trace_details_end = script.index("\n\t\t// Per-{baseUrl, sessionId}", trace_details_start)
    trace_functions = (
        script[trace_validation_start:trace_validation_end]
        + "\n"
        + script[trace_details_start:trace_details_end]
    )
    harness = textwrap.dedent(
        """
        const state = { traceId: null };
        const TRACE_ID_PATTERN = /^[0-9a-f]{32}$/i;
        const runDetailsBarEl = { hidden: true };
        const traceIdValueEl = { textContent: "", title: "" };
        const statuses = [];
        let copiedValue = null;
        Object.defineProperty(globalThis, "navigator", {
          configurable: true,
          value: {
            clipboard: {
              writeText: async (value) => { copiedValue = value; }
            }
          }
        });
        function setStatus(value, isError) { statuses.push({ value, isError }); }

        __TRACE_FUNCTIONS__

        const traceId = "a".repeat(32);
        setLatestTraceId(traceId);
        if (state.traceId !== traceId || runDetailsBarEl.hidden) {
          throw new Error("trace run detail did not render");
        }
        if (traceIdValueEl.textContent !== traceId || traceIdValueEl.title !== traceId) {
          throw new Error("trace run detail did not expose the response trace ID");
        }
        if (getTraceId({ headers: { get: () => traceId } }) !== traceId) {
          throw new Error("valid response trace ID was rejected");
        }
        if (getTraceId({ headers: { get: () => "provider-response-id" } }) !== null) {
          throw new Error("non-trace response header was exposed");
        }
        await copyTraceId();
        if (copiedValue !== traceId || !statuses.at(-1).value.includes("Run trace ID copied")) {
          throw new Error("trace run detail was not copyable");
        }
        setLatestTraceId(null);
        if (state.traceId !== null || !runDetailsBarEl.hidden || traceIdValueEl.textContent !== "") {
          throw new Error("trace run detail did not reset");
        }
        """
    ).replace("__TRACE_FUNCTIONS__", trace_functions)

    _run_node(harness)

    send_start = script.index("async function sendMessage(")
    send_end = script.index('\n\t\tcomposerEl.addEventListener("submit"', send_start)
    send_block = script[send_start:send_end]
    assert "setLatestTraceId(null);" in send_block
    assert "setLatestFhaSessionId(null);" in send_block
    assert "setLatestRunMetrics(null);" in send_block
    assert "setLatestTraceId(getTraceId(response));" in send_block
    assert "setLatestFhaSessionId(getFhaSessionId(response));" in send_block
    assert 'response?.headers?.get("x-ms-fha-session-id")' in script
    assert 'response.headers.get("x-ms-session-id")' in send_block
    assert "state.sessionId = normalizedSessionId;" in send_block
    assert "updateSessionBar();" in send_block

    new_session_start = script.index('newSessionBtnEl.addEventListener("click"')
    new_session_end = script.index('settingsBtnEl.addEventListener("click"', new_session_start)
    assert "setLatestTraceId(null);" in script[new_session_start:new_session_end]
    assert "setLatestFhaSessionId(null);" in script[new_session_start:new_session_end]
    assert "setLatestRunMetrics(null);" in script[new_session_start:new_session_end]


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_fha_session_detail_renders_copies_and_resets() -> None:
    script = _script_text()
    start = script.index("function updateFhaSessionBar()")
    end = script.index("\n\t\tfunction updateRunDetailsBar()", start)
    functions = script[start:end]
    harness = textwrap.dedent(
        """
        const state = { fhaSessionId: null };
        const FHA_SESSION_ID_PATTERN = /^fhs1-[a-z2-7]{52}$/;
        const fhaSessionBarEl = { hidden: true };
        const fhaSessionIdValueEl = { textContent: "", title: "" };
        const statuses = [];
        let copiedValue = null;
        function isFhaSessionId(value) {
          return typeof value === "string" && FHA_SESSION_ID_PATTERN.test(value);
        }
        async function writeClipboardText(value) {
          copiedValue = value;
          return true;
        }
        function setStatus(value, isError) { statuses.push({ value, isError }); }

        __FHA_SESSION_FUNCTIONS__

        const providerSessionId = `fhs1-${"a".repeat(52)}`;
        setLatestFhaSessionId(providerSessionId);
        if (state.fhaSessionId !== providerSessionId || fhaSessionBarEl.hidden) {
          throw new Error("FHA session detail did not render");
        }
        if (
          fhaSessionIdValueEl.textContent !== providerSessionId
          || fhaSessionIdValueEl.title !== providerSessionId
        ) {
          throw new Error("FHA session ID was not exposed");
        }
        if (
          getFhaSessionId({ headers: { get: () => providerSessionId } })
          !== providerSessionId
        ) {
          throw new Error("valid FHA session response header was rejected");
        }
        await copyFhaSessionId();
        if (
          copiedValue !== providerSessionId
          || !statuses.at(-1).value.includes("FHA session ID copied")
        ) {
          throw new Error("FHA session detail was not copyable");
        }
        setLatestFhaSessionId(null);
        if (
          state.fhaSessionId !== null
          || !fhaSessionBarEl.hidden
          || fhaSessionIdValueEl.textContent !== ""
        ) {
          throw new Error("FHA session detail did not reset");
        }
        """
    ).replace("__FHA_SESSION_FUNCTIONS__", functions)

    _run_node(harness)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_run_metrics_render_copy_and_reset() -> None:
    script = _script_text()
    start = script.index("async function writeClipboardText(")
    end = script.index("\n\t\t// Per-{baseUrl, sessionId}", start)
    functions = script[start:end]
    harness = textwrap.dedent(
        """
        const state = {
          runMetrics: null,
          sessionId: "session-1",
          fhaSessionId: `fhs1-${"b".repeat(52)}`,
          traceId: "a".repeat(32),
        };
        const runMetricsBarEl = { hidden: true };
        const totalTimeValueEl = { textContent: "" };
        const firstOutputValueEl = { textContent: "" };
        const streamTimeValueEl = { textContent: "" };
        const deltaCountValueEl = { textContent: "" };
        const sessionModeValueEl = { textContent: "" };
        const statuses = [];
        let copiedValue = null;
        Object.defineProperty(globalThis, "navigator", {
          configurable: true,
          value: {
            clipboard: {
              writeText: async (value) => { copiedValue = value; }
            }
          }
        });
        function setStatus(value, isError) { statuses.push({ value, isError }); }

        __METRIC_FUNCTIONS__

        setLatestRunMetrics({
          totalMs: 11655,
          firstOutputMs: 10015,
          streamingMs: 1639,
          deltaCount: 77,
          sessionMode: "continued",
        });
        if (runMetricsBarEl.hidden) throw new Error("metrics bar did not render");
        if (totalTimeValueEl.textContent !== "11.7 s") throw new Error("total time mismatch");
        if (firstOutputValueEl.textContent !== "10.0 s") throw new Error("first output mismatch");
        if (streamTimeValueEl.textContent !== "1.64 s") throw new Error("stream time mismatch");
        if (deltaCountValueEl.textContent !== "77") throw new Error("delta count mismatch");
        if (sessionModeValueEl.textContent !== "Continued") throw new Error("turn mode mismatch");

        await copyRunMetrics();
        if (!copiedValue.includes("Total response: 11.7 s")) throw new Error("total not copied");
        if (!copiedValue.includes("First output: 10.0 s")) throw new Error("TTFT not copied");
        if (!copiedValue.includes("Session ID: session-1")) throw new Error("session not copied");
        if (!copiedValue.includes(`FHA session ID: fhs1-${"b".repeat(52)}`)) {
          throw new Error("FHA session not copied");
        }
        if (!copiedValue.includes(`Run trace ID: ${"a".repeat(32)}`)) {
          throw new Error("trace not copied");
        }
        if (!statuses.at(-1).value.includes("Run performance copied")) {
          throw new Error("copy status missing");
        }

        setLatestRunMetrics(null);
        if (!runMetricsBarEl.hidden || state.runMetrics !== null) {
          throw new Error("metrics bar did not reset");
        }
        """
    ).replace("__METRIC_FUNCTIONS__", functions)

    _run_node(harness)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_stream_reader_keeps_repeated_deltas_and_accepts_done_content() -> None:
    send_message = _send_message_function(_script_text())
    harness = textwrap.dedent(
        """
        const state = {
          baseUrl: "https://example.test",
          key: "",
          sessionId: null,
          fhaSessionId: null,
          traceId: null,
        };
        let lastSentUserMessage = "repeat";
        let sessionIdIsExplicit = false;
        let sessionWasFromServer = false;
        let restoredSessionIdCandidate = null;
        let workflowsCapability = "unsupported";
        const deltas = [];
        let now = 100;
        Object.defineProperty(globalThis, "performance", {
          configurable: true,
          value: { now: () => now }
        });

        function setLatestTraceId() {}
        function setLatestFhaSessionId(value) { state.fhaSessionId = value; }
        function setLatestRunMetrics() {}
        function getApiBasePath() { return "/agents/main"; }
        function getTraceId() { return null; }
        function getFhaSessionId(response) {
          return response.headers.get("x-ms-fha-session-id");
        }
        function isValidSessionId(value) { return Boolean(value); }
        function persistSessionId() {}
        function updateSessionBar() {}
        function recordSessionActivity() {}
        function startWorkflowPolling() {}
        function probeWorkflowsCapability() {}
        function normalizeDetails(details) { return details; }
        function getMessageFromBody(body) { return String(body); }

        const encoder = new TextEncoder();
        const payloads = [
          'id: 1\\ndata: {"type":"session","session_id":"session-1","status":"running"}\\n\\n',
          'id: 2\\ndata: {"type":"delta","content":"ha"}\\n\\n',
          'id: 3\\ndata: {"type":"delta","content":"ha"}\\n\\n',
          'id: 4\\ndata: {"type":"done","content":"haha"}\\n\\n',
        ];
        const deliveryTimes = [150, 300, 400, 550];
        let delivered = 0;
        globalThis.fetch = async () => ({
          ok: true,
          status: 200,
          headers: {
            get: (name) => {
              if (name === "x-ms-session-id") return "session-1";
              if (name === "x-ms-fha-session-id") return `fhs1-${"c".repeat(52)}`;
              return null;
            }
          },
          body: {
            getReader: () => ({
              read: async () => {
                if (delivered >= payloads.length) return { value: undefined, done: true };
                now = deliveryTimes[delivered];
                return { value: encoder.encode(payloads[delivered++]), done: false };
              }
            })
          }
        });

        __SEND_MESSAGE__

        const result = await sendMessage("repeat", {
          startedAt: 100,
          onDelta: (text) => deltas.push(text),
        });
        if (deltas.length !== 2 || deltas[0] !== "ha" || deltas[1] !== "haha") {
          throw new Error(`repeated deltas were not preserved: ${JSON.stringify(deltas)}`);
        }
        if (result.text !== "haha" || result.sessionId !== "session-1") {
          throw new Error(`unexpected stream result: ${JSON.stringify(result)}`);
        }
        if (state.fhaSessionId !== `fhs1-${"c".repeat(52)}`) {
          throw new Error("FHA session header was not adopted");
        }
        if (
          result.metrics.totalMs !== 450
          || result.metrics.firstOutputMs !== 200
          || result.metrics.streamingMs !== 250
          || result.metrics.deltaCount !== 2
          || result.metrics.sessionMode !== "new"
        ) {
          throw new Error(`unexpected stream metrics: ${JSON.stringify(result.metrics)}`);
        }
        """
    ).replace("__SEND_MESSAGE__", send_message)

    _run_node(harness)
