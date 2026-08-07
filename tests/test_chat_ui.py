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
