from __future__ import annotations

import contextlib
import functools
import http.server
import shutil
import threading
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest

from tests.doubles.durable_chat_cdp import chromium_page

_WORKSPACE = Path(__file__).resolve().parents[1]
_HISTORY_SOURCE = (
    _WORKSPACE
    / "src"
    / "azure_functions_agents"
    / "public"
    / "durable-chat"
    / "history.js"
)
_ARTIFACT_ROOT = _WORKSPACE / ".dch"


class _QuietStaticHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, _: str, *args: object) -> None:
        return


@contextlib.contextmanager
def _history_page() -> Iterator[str]:
    directory = _ARTIFACT_ROOT / uuid4().hex
    directory.mkdir(parents=True)
    shutil.copyfile(_HISTORY_SOURCE, directory / "history.js")
    (directory / "index.html").write_text(
        """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Durable chat history test</title></head>
<body>
<script type="module">
  import { openDurableChatHistory } from "./history.js";
  window.durableChatHistory = { openDurableChatHistory };
</script>
</body>
</html>
""",
        encoding="utf-8",
    )
    handler = functools.partial(_QuietStaticHandler, directory=str(directory))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    host, port = server.server_address[:2]

    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)
        shutil.rmtree(directory, ignore_errors=True)
        with contextlib.suppress(OSError):
            _ARTIFACT_ROOT.rmdir()


@pytest.mark.asyncio
async def test_indexeddb_history_preserves_submission_and_fences_stale_writes() -> None:
    with _history_page() as page_url:
        browser_directory = _ARTIFACT_ROOT / f"browser-{uuid4().hex}"
        try:
            async with chromium_page(browser_directory) as page:
                await page.navigate(f"{page_url}/index.html")
                await page.wait_for("window.durableChatHistory !== undefined")
                result = await page.evaluate(
                """(async () => {
                  const { openDurableChatHistory } = window.durableChatHistory;
                  const namespace = `deployment-route-agent-owner-${crypto.randomUUID()}`;
                  const primary = await openDurableChatHistory({ namespace });
                  const secondary = await openDurableChatHistory({ namespace });

                  const created = await primary.createSession({
                    sessionId: "session-1",
                    title: "Initial title",
                  });
                  const submission = {
                    prompt: "Preserve this user prompt exactly.",
                    nested: { text: "Do not redact user content." },
                  };
                  const recorded = await primary.recordSubmission({
                    sessionId: "session-1",
                    requestId: "request-1",
                    normalizedSubmission: submission,
                    idempotencyKey: "idempotency-1",
                    transcript: [{ role: "user", text: submission.prompt }],
                    draft: { text: "Draft one" },
                    projection: { state: "submitted" },
                  });
                  submission.prompt = "Mutated after persistence.";
                  const duplicate = await primary.recordSubmission({
                    sessionId: "session-1",
                    requestId: "request-1",
                    normalizedSubmission: {
                      prompt: "Preserve this user prompt exactly.",
                      nested: { text: "Do not redact user content." },
                    },
                    idempotencyKey: "idempotency-1",
                  });

                  const applied = await primary.applyProjection({
                    sessionId: "session-1",
                    requestId: "request-1",
                    cursor: {
                      position: 7,
                      revision: 0,
                      value: { opaque: "cursor-seven" },
                    },
                    projection: { state: "running", text: "Draft one" },
                    transcript: [{ role: "user", text: "Preserve this user prompt exactly." }],
                    draft: { text: "Draft one" },
                  });
                  const stale = await secondary.applyProjection({
                    sessionId: "session-1",
                    requestId: "request-1",
                    cursor: { position: 6, revision: 0, value: { opaque: "cursor-six" } },
                    projection: { state: "stale" },
                    terminal: false,
                  });
                  const terminal = await primary.applyProjection({
                    sessionId: "session-1",
                    requestId: "request-1",
                    cursor: { position: 8, revision: 0, value: { opaque: "cursor-eight" } },
                    projection: { state: "completed", result: "Final response" },
                    transcript: [{ role: "assistant", text: "Final response" }],
                    draft: null,
                    terminal: true,
                    expectedVersion: applied.request.version,
                  });
                  const reverted = await secondary.applyProjection({
                    sessionId: "session-1",
                    requestId: "request-1",
                    cursor: { position: 9, revision: 0, value: { opaque: "cursor-nine" } },
                    projection: { state: "running again" },
                    terminal: false,
                  });
                  const snapshot = await primary.applyProjection({
                    sessionId: "session-1",
                    requestId: "request-1",
                    cursor: { position: 9, revision: 1, value: { opaque: "cursor-nine" } },
                    projection: { state: "completed", result: "Snapshot replacement" },
                    transcript: [{ role: "assistant", text: "Snapshot replacement" }],
                    draft: null,
                    terminal: true,
                    expectedVersion: terminal.request.version,
                    mode: "snapshot",
                  });

                  let sensitiveFieldCode = "";
                  try {
                    await primary.recordSubmission({
                      sessionId: "session-1",
                      requestId: "request-with-header",
                      normalizedSubmission: {
                        prompt: "Do not drop this prompt.",
                        headers: { Authorization: "not-persisted" },
                      },
                      idempotencyKey: "idempotency-2",
                    });
                  } catch (error) {
                    sensitiveFieldCode = error.code;
                  }

                  const storedBeforeRemoval = await primary.getRequest("session-1", "request-1");
                  const sessionsBeforeRename = await primary.listSessions();
                  const renamed = await primary.renameSession({
                    sessionId: "session-1",
                    title: "Renamed session",
                    expectedVersion: sessionsBeforeRename.sessions[0].version,
                  });
                  let confirmationCode = "";
                  try {
                    await primary.removeSession({
                      sessionId: "session-1",
                      confirmed: false,
                      expectedVersion: renamed.session.version,
                    });
                  } catch (error) {
                    confirmationCode = error.code;
                  }
                  const removal = await primary.removeSession({
                    sessionId: "session-1",
                    confirmed: true,
                    expectedVersion: renamed.session.version,
                  });
                  const sessionsAfterRemoval = await primary.listSessions();
                  const requestsAfterRemoval = await primary.listRequests("session-1");
                  primary.close();
                  secondary.close();

                  return {
                    created,
                    recorded,
                    duplicate,
                    applied,
                    stale,
                    terminal,
                    reverted,
                    snapshot,
                    sensitiveFieldCode,
                    storedBeforeRemoval,
                    renamed,
                    confirmationCode,
                    removal,
                    sessionsAfterRemoval,
                    requestsAfterRemoval,
                  };
                })()""",
                )
        finally:
            shutil.rmtree(browser_directory, ignore_errors=True)

    assert result["created"]["mode"] == "persistent"
    assert result["recorded"]["disposition"] == "recorded"
    assert result["duplicate"]["disposition"] == "existing"
    assert result["storedBeforeRemoval"]["request"]["normalizedSubmission"]["prompt"] == (
        "Preserve this user prompt exactly."
    )
    assert result["storedBeforeRemoval"]["request"]["idempotencyKey"] == "idempotency-1"
    assert result["applied"]["request"]["cursor"]["position"] == 7
    assert result["stale"]["disposition"] == "stale_cursor"
    assert result["terminal"]["request"]["terminal"] is True
    assert result["reverted"]["disposition"] == "terminal_preserved"
    assert result["snapshot"]["request"]["cursor"] == {
        "position": 9,
        "revision": 1,
        "value": {"opaque": "cursor-nine"},
    }
    assert result["snapshot"]["request"]["projection"] == {
        "state": "completed",
        "result": "Snapshot replacement",
    }
    assert result["sensitiveFieldCode"] == "sensitive_field"
    assert result["renamed"]["session"]["title"] == "Renamed session"
    assert result["confirmationCode"] == "confirmation_required"
    assert result["removal"]["disposition"] == "removed"
    assert result["removal"]["removedRequests"] == 1
    assert result["sessionsAfterRemoval"]["sessions"] == []
    assert result["requestsAfterRemoval"]["requests"] == []


@pytest.mark.asyncio
async def test_history_reports_unsupported_schema_and_requires_volatile_announcement() -> None:
    with _history_page() as page_url:
        browser_directory = _ARTIFACT_ROOT / f"failure-{uuid4().hex}"
        try:
            async with chromium_page(browser_directory) as page:
                await page.navigate(f"{page_url}/index.html")
                await page.wait_for("window.durableChatHistory !== undefined")
                result = await page.evaluate(
                    """(async () => {
                      const { openDurableChatHistory } = window.durableChatHistory;
                      const namespace = `newer-schema-${crypto.randomUUID()}`;
                      const databaseName = `azure-functions-agents.durable-chat.history.${namespace}`;
                      await new Promise((resolve, reject) => {
                        const request = indexedDB.open(databaseName, 2);
                        request.onupgradeneeded = () => request.result.createObjectStore("preserved");
                        request.onsuccess = () => {
                          request.result.close();
                          resolve();
                        };
                        request.onerror = () => reject(request.error);
                      });

                      let unsupportedSchemaCode = "";
                      try {
                        await openDurableChatHistory({ namespace });
                      } catch (error) {
                        unsupportedSchemaCode = error.code;
                      }
                      const retainedVersion = await new Promise((resolve, reject) => {
                        const request = indexedDB.open(databaseName);
                        request.onsuccess = () => {
                          const version = request.result.version;
                          request.result.close();
                          resolve(version);
                        };
                        request.onerror = () => reject(request.error);
                      });

                      const hadOwnIndexedDb = Object.prototype.hasOwnProperty.call(
                        globalThis,
                        "indexedDB",
                      );
                      const indexedDbDescriptor = hadOwnIndexedDb
                        ? Object.getOwnPropertyDescriptor(globalThis, "indexedDB")
                        : undefined;
                      Object.defineProperty(globalThis, "indexedDB", {
                        configurable: true,
                        value: undefined,
                        writable: true,
                      });

                      try {
                        let missingAnnouncerCode = "";
                        try {
                          await openDurableChatHistory({
                            namespace: `volatile-no-announcer-${crypto.randomUUID()}`,
                            allowVolatile: true,
                          });
                        } catch (error) {
                          missingAnnouncerCode = error.code;
                        }
                        const announcements = [];
                        const volatile = await openDurableChatHistory({
                          namespace: `volatile-announced-${crypto.randomUUID()}`,
                          allowVolatile: true,
                          announceVolatileMode: (failure) => announcements.push(failure),
                        });
                        const mutation = await volatile.createSession({
                          sessionId: "volatile-session",
                          title: "Not persisted",
                        });
                        volatile.close();

                        return {
                          announcements,
                          missingAnnouncerCode,
                          mutation,
                          retainedVersion,
                          unsupportedSchemaCode,
                          volatileMode: volatile.mode,
                          volatileRequiresAnnouncement: volatile.requiresAnnouncement,
                        };
                      } finally {
                        if (indexedDbDescriptor) {
                          Object.defineProperty(globalThis, "indexedDB", indexedDbDescriptor);
                        } else {
                          delete globalThis.indexedDB;
                        }
                      }
                    })()""",
                )
        finally:
            shutil.rmtree(browser_directory, ignore_errors=True)

    assert result["unsupportedSchemaCode"] == "unsupported_schema"
    assert result["retainedVersion"] == 2
    assert result["missingAnnouncerCode"] == "volatile_announcer_required"
    assert result["announcements"] == [
        {
            "code": "storage_unavailable",
            "message": "IndexedDB is unavailable in this browser context.",
        }
    ]
    assert result["volatileMode"] == "volatile"
    assert result["volatileRequiresAnnouncement"] is True
    assert result["mutation"]["mode"] == "volatile"
    assert result["mutation"]["disposition"] == "created"
