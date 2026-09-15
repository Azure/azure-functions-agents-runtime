const DETAILS_PREFERENCE_KEY = "azure-functions-agents.durable-chat.details-visible";

const CONNECTION_COPY = {
  unconfigured: {
    summary: "Connection not configured",
    notice: "Connect to a Function App to load or start a session.",
  },
  loading: {
    summary: "Connecting",
    notice: "Connecting to the Function App.",
  },
  ready: {
    summary: "Connection ready",
    notice: "The Function App connection is ready.",
  },
  reconnecting: {
    summary: "Reconnecting",
    notice: "The connection was interrupted. Reconnecting.",
  },
  error: {
    summary: "Connection unavailable",
    notice: "The Function App connection could not be established.",
  },
};

const STATUS_LABELS = {
  submitting: "Submitting",
  queued: "Queued",
  running: "Running",
  waiting: "Needs input",
  completed: "Completed",
  cancel_requested: "Cancel requested",
  cancelled: "Cancelled",
  failed: "Failed",
  session_busy: "Session busy",
  uncertain: "Start uncertain",
  expired: "Expired",
  reconnecting: "Reconnecting",
  degraded: "Observation limited",
  unknown: "Status unavailable",
};

const PROGRESS_STATES = new Set(["pending", "running", "completed", "waiting", "failed"]);
const CONNECTION_STATES = new Set(Object.keys(CONNECTION_COPY));
const SENSITIVE_DIAGNOSTIC_QUERY_KEYS = new Set([
  "access_token",
  "accountkey",
  "apikey",
  "authorization",
  "code",
  "credential",
  "functionkey",
  "key",
  "password",
  "secret",
  "sharedaccesskey",
  "sharedaccesssignature",
  "sig",
  "signature",
  "token",
  "x-functions-key",
]);

let activeShell;

/**
 * A network-independent renderer for Durable Agent Chat.
 *
 * This module deliberately owns only safe DOM rendering and local shell
 * interactions. The application module supplies normalized session/request
 * data, listens for the documented durable-chat:* events, and owns HTTP, SSE,
 * IndexedDB, authentication, and request-state transitions.
 *
 * @typedef {object} DurableChatRequest
 * @property {string} id
 * @property {string} [prompt]
 * @property {string|{text?: string, mode?: "foreground"|"background", draft?: boolean}} [response]
 * @property {keyof typeof STATUS_LABELS} [status]
 * @property {boolean} [canCancel]
 * @property {boolean} [completionPending]
 * @property {{message?: string}} [error]
 * @property {Array<{label?: string, detail?: string, state?: string, timestamp?: string}>} [progress]
 * @property {{id?: string, prompt?: string, choices?: Array<{label?: string, value?: string}>, allowFreeText?: boolean, schema?: {label?: string, description?: string, value?: string}}} [humanInput]
 * @property {string} [runId]
 * @property {string} [sandboxGroup]
 * @property {{id?: string, group?: string, history?: Array<string|{label?: string, detail?: string}>}} [sandbox]
 * @property {{dtsUrl?: string, dtsReason?: string, appInsightsUrl?: string, appInsightsReason?: string, note?: string}} [diagnostics]
 */

/**
 * Mount the shell below a document or the shell element itself.
 *
 * The returned controller exposes:
 * - setConnectionState({ state, message, deploymentLabel, configuredSandboxGroup, allowKeyEntry })
 * - setConnectionPanelVisible(visible)
 * - renderSessions({ sessions, selectedSessionId, state, error, historyMode })
 * - renderSession({ id, title, status, composer })
 * - renderRequests({ requests, selectedRequestId, state, error, revealSelected })
 * - selectRequest(requestId, { focus, reveal })
 * - beginSessionRename(sessionId)
 * - renderInspector(request)
 * - setComposer({ enabled, requestId, cancelable, message })
 * - setSandboxProfiles({ profiles, selectedProfile, enabled })
 * - setStorageNotice({ visible, message })
 * - getFunctionKey(), clearFunctionKey(), announce(message), destroy()
 *
 * UI events bubble from #durable-chat-shell: durable-chat:new-session,
 * durable-chat:select-session, durable-chat:remove-session,
 * durable-chat:submit-message, durable-chat:cancel-request,
 * durable-chat:respond-human-input, durable-chat:reconnect,
 * durable-chat:submit-function-key, durable-chat:clear-function-key,
 * durable-chat:copy-identifier, durable-chat:details-visibility-change,
 * durable-chat:request-selected, durable-chat:rename-session, and
 * durable-chat:commit-session-rename, and durable-chat:sandbox-profile-change.
 *
 * @param {Document|Element} root
 * @returns {ReturnType<typeof createController>|null}
 */
export function createDurableChatShell(root = document) {
  const shell = findShell(root);
  if (!shell) {
    return null;
  }

  if (shell.__durableChatController) {
    return shell.__durableChatController;
  }

  const elements = getElements(shell);
  const state = {
    abortController: new AbortController(),
    mediaQuery: window.matchMedia("(max-width: 800px)"),
    requests: new Map(),
    selectedRequestId: null,
  };

  const controller = createController(shell, elements, state);
  shell.__durableChatController = controller;
  installEventHandlers(shell, elements, state, controller);
  controller.setDetailsVisible(readDetailsPreference(), { persist: false });

  activeShell = controller;
  return controller;
}

export function getDurableChatShell() {
  return activeShell;
}

function createController(shell, elements, state) {
  return {
    elements,
    announce(message) {
      setText(elements.announcements, message);
    },
    clearFunctionKey() {
      elements.functionsKey.value = "";
    },
    destroy() {
      state.abortController.abort();
      removeMediaListener(state.mediaQuery, state.onMediaChange);
      state.requests.clear();
      if (activeShell === this) {
        activeShell = undefined;
      }
      delete shell.__durableChatController;
    },
    getFunctionKey() {
      return elements.functionsKey.value;
    },
    renderInspector(request) {
      renderInspector(elements, request);
    },
    renderRequests(options = {}) {
      renderRequests(shell, elements, state, this, options);
    },
    renderSession(options = {}) {
      renderSession(elements, this, options);
    },
    renderSessions(options = {}) {
      renderSessions(shell, elements, options);
    },
    setConnectionPanelVisible(visible) {
      setConnectionPanelVisible(elements, Boolean(visible));
    },
    selectRequest(requestId, options = {}) {
      return selectRequest(shell, elements, state, this, requestId, options);
    },
    beginSessionRename(sessionId) {
      beginSessionRename(shell, elements, sessionId);
    },
    setComposer(options = {}) {
      setComposer(elements, options);
    },
    setConnectionState(options = {}) {
      setConnectionState(shell, elements, options);
    },
    setDetailsVisible(visible, options = {}) {
      setDetailsVisible(shell, elements, Boolean(visible), options);
    },
    setSandboxProfiles(options = {}) {
      setSandboxProfiles(elements, options);
    },
    setSidebarOpen(open) {
      setSidebarOpen(shell, elements, Boolean(open));
    },
    setStorageNotice(options = {}) {
      setStorageNotice(elements, options);
    },
  };
}

function findShell(root) {
  if (root instanceof Element && root.id === "durable-chat-shell") {
    return root;
  }

  return root.querySelector("#durable-chat-shell");
}

function getElements(shell) {
  const byId = (id) => {
    const element = shell.querySelector(`#${id}`);
    if (!element) {
      throw new Error(`Durable Chat shell is missing #${id}.`);
    }
    return element;
  };

  return {
    announcements: byId("durable-chat-announcements"),
    authClear: byId("durable-chat-auth-clear"),
    authForm: byId("durable-chat-auth-form"),
    authNote: byId("durable-chat-auth-note"),
    authSubmit: byId("durable-chat-auth-submit"),
    cancelRequest: byId("durable-chat-cancel-request"),
    composer: byId("durable-chat-composer"),
    composerState: byId("durable-chat-composer-state"),
    connectionMessage: byId("durable-chat-connection-message"),
    connectionNotice: byId("durable-chat-connection-notice"),
    connectionNoticeText: byId("durable-chat-connection-notice-text"),
    connectionPanel: byId("durable-chat-connection-panel"),
    connectionSummary: byId("durable-chat-connection-summary"),
    connectionToggle: byId("durable-chat-connection-toggle"),
    configuredSandboxGroup: byId("durable-chat-configured-sandbox-group"),
    configuredSandboxGroupContext: byId("durable-chat-configured-sandbox-group-context"),
    conversationError: byId("durable-chat-conversation-error"),
    conversationErrorText: byId("durable-chat-conversation-error-text"),
    conversationLoading: byId("durable-chat-conversation-loading"),
    copyRunId: byId("durable-chat-copy-run-id"),
    copySandboxId: byId("durable-chat-copy-sandbox-id"),
    copySandboxGroup: byId("durable-chat-copy-sandbox-group"),
    copySessionId: byId("durable-chat-copy-session-id"),
    deploymentLabel: byId("durable-chat-deployment-label"),
    detailsHeading: byId("durable-chat-details-heading"),
    detailsPanel: byId("durable-chat-details-panel"),
    detailsSelection: byId("durable-chat-details-selection"),
    detailsStatus: byId("durable-chat-details-status"),
    detailsToggle: byId("durable-chat-details-toggle"),
    detailsToggleLabel: byId("durable-chat-details-toggle-label"),
    diagnosticsNote: byId("durable-chat-diagnostics-note"),
    emptyState: byId("durable-chat-empty-state"),
    functionsKey: byId("durable-chat-functions-key"),
    historyStatus: byId("durable-chat-history-status"),
    historyStatusText: byId("durable-chat-history-status-text"),
    lifecycleProgress: byId("durable-chat-lifecycle-progress"),
    openAppInsights: byId("durable-chat-open-app-insights"),
    openDts: byId("durable-chat-open-dts"),
    progressSummary: byId("durable-chat-progress-summary"),
    prompt: byId("durable-chat-prompt"),
    requestList: byId("durable-chat-request-list"),
    runId: byId("durable-chat-run-id"),
    sandboxGroup: byId("durable-chat-sandbox-group"),
    sandboxGroupLabel: byId("durable-chat-sandbox-group-label"),
    sandboxHistory: byId("durable-chat-sandbox-history"),
    sandboxId: byId("durable-chat-sandbox-id"),
    send: byId("durable-chat-send"),
    sessionCount: byId("durable-chat-session-count"),
    sessionId: byId("durable-chat-session-id"),
    sessionList: byId("durable-chat-session-list"),
    sessionListState: byId("durable-chat-session-list-state"),
    sessionStatus: byId("durable-chat-session-status"),
    sessionTitle: byId("durable-chat-session-title"),
    sidebar: byId("durable-chat-sidebar"),
    sidebarScrim: byId("durable-chat-sidebar-scrim"),
    sidebarToggle: byId("durable-chat-sidebar-toggle"),
    sandboxProfile: byId("durable-chat-sandbox-profile"),
    storageNotice: byId("durable-chat-storage-notice"),
    transcript: byId("durable-chat-transcript"),
  };
}

function installEventHandlers(shell, elements, state, controller) {
  const { signal } = state.abortController;

  shell.addEventListener("click", (event) => {
    const control = event.target.closest("[data-action]");
    if (!control || !shell.contains(control) || control.disabled) {
      return;
    }

    const action = control.dataset.action;
    switch (action) {
      case "new-session":
        emit(shell, "durable-chat:new-session");
        break;
      case "select-session":
        emit(shell, "durable-chat:select-session", { sessionId: control.dataset.sessionId });
        setSidebarOpen(shell, elements, false);
        break;
      case "remove-session":
        emit(shell, "durable-chat:remove-session", { sessionId: control.dataset.sessionId });
        break;
      case "rename-session":
        emit(shell, "durable-chat:rename-session", { sessionId: control.dataset.sessionId });
        break;
      case "cancel-session-rename":
        cancelSessionRename(shell, control.dataset.sessionId);
        break;
      case "view-details":
        controller.selectRequest(control.dataset.requestId, { focus: true });
        break;
      case "cancel-request":
        emit(shell, "durable-chat:cancel-request", { requestId: control.dataset.requestId });
        break;
      case "reconnect":
        emit(shell, "durable-chat:reconnect");
        break;
      case "copy-identifier":
        copyIdentifier(shell, elements, control);
        break;
      case "human-choice":
        emit(shell, "durable-chat:respond-human-input", {
          inputId: control.dataset.humanInputId,
          requestId: control.dataset.requestId,
          response: control.dataset.answer,
          responseType: "choice",
        });
        break;
      default:
        break;
    }
  }, { signal });

  elements.detailsToggle.addEventListener("click", () => {
    controller.setDetailsVisible(shell.dataset.detailsVisible !== "true");
  }, { signal });

  elements.connectionToggle.addEventListener("click", () => {
    const expanded = elements.connectionToggle.getAttribute("aria-expanded") === "true";
    elements.connectionToggle.setAttribute("aria-expanded", String(!expanded));
    elements.connectionPanel.hidden = expanded;
  }, { signal });

  elements.sidebarToggle.addEventListener("click", () => {
    controller.setSidebarOpen(shell.dataset.sidebarOpen !== "true");
  }, { signal });

  elements.sidebarScrim.addEventListener("click", () => {
    controller.setSidebarOpen(false);
  }, { signal });

  elements.sandboxProfile.addEventListener("change", () => {
    emit(shell, "durable-chat:sandbox-profile-change", {
      sandboxProfile: elements.sandboxProfile.value,
    });
  }, { signal });

  elements.authClear.addEventListener("click", () => {
    controller.clearFunctionKey();
    emit(shell, "durable-chat:clear-function-key");
  }, { signal });

  shell.addEventListener("submit", (event) => {
    const form = event.target;
    event.preventDefault();

    if (form === elements.composer) {
      submitComposer(shell, elements);
      return;
    }

    if (form === elements.authForm) {
      emit(shell, "durable-chat:submit-function-key");
      return;
    }

    if (form.matches("[data-human-input-form]")) {
      submitHumanInput(shell, form, elements);
      return;
    }

    if (form.matches("[data-session-rename-form]")) {
      const title = form.elements.namedItem("title");
      emit(shell, "durable-chat:commit-session-rename", {
        sessionId: form.dataset.sessionId,
        title: title instanceof HTMLInputElement ? title.value : "",
      });
    }
  }, { signal });

  elements.prompt.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || event.shiftKey || event.isComposing || elements.prompt.disabled) {
      return;
    }

    event.preventDefault();
    elements.composer.requestSubmit();
  }, { signal });

  state.onMediaChange = (event) => {
    if (!event.matches) {
      setSidebarOpen(shell, elements, false);
    }
  };
  addMediaListener(state.mediaQuery, state.onMediaChange);
}

function setConnectionState(shell, elements, options) {
  const connectionState = CONNECTION_STATES.has(options.state) ? options.state : "unconfigured";
  const copy = CONNECTION_COPY[connectionState];
  const summary = textOr(options.summary, copy.summary);
  const notice = textOr(options.notice ?? options.message, copy.notice);

  shell.dataset.connectionState = connectionState;
  setText(elements.connectionSummary.lastElementChild, summary);
  elements.connectionNotice.dataset.state = connectionState;
  setText(elements.connectionNoticeText, notice);
  renderConfiguredSandboxGroup(elements, options.configuredSandboxGroup);
  setText(elements.connectionMessage, textOr(options.message, notice));

  if (typeof options.deploymentLabel === "string") {
    setText(elements.deploymentLabel, options.deploymentLabel || "Function App");
  }

  const allowKeyEntry = options.allowKeyEntry === true;
  elements.authForm.hidden = !allowKeyEntry;
  elements.authNote.hidden = !allowKeyEntry;
  elements.functionsKey.disabled = !allowKeyEntry;
  elements.authSubmit.disabled = !allowKeyEntry;
  elements.authClear.disabled = !allowKeyEntry;

  if (options.announce === true) {
    setText(elements.announcements, notice);
  }
}

function setConnectionPanelVisible(elements, visible) {
  elements.connectionPanel.hidden = !visible;
  elements.connectionToggle.setAttribute("aria-expanded", String(visible));
}

function renderConfiguredSandboxGroup(elements, value) {
  const group = stringOrEmpty(value);
  elements.configuredSandboxGroupContext.hidden = !group;
  setText(elements.configuredSandboxGroup, group || "—");
}

function renderSessions(shell, elements, options) {
  const sessions = Array.isArray(options.sessions) ? options.sessions.filter(hasId) : [];
  const selectedSessionId = stringOrEmpty(options.selectedSessionId);
  const listState = ["loading", "error", "ready"].includes(options.state) ? options.state : "ready";

  clear(elements.sessionList);
  setText(elements.sessionCount, listState === "loading" ? "…" : String(sessions.length));
  renderHistoryStatus(elements, options.historyMode);

  if (listState === "loading") {
    setText(elements.sessionListState, "Loading saved sessions.");
  } else if (listState === "error") {
    setText(elements.sessionListState, textOr(options.error, "Saved sessions are unavailable."));
  } else if (sessions.length === 0) {
    setText(elements.sessionListState, "No saved sessions are available yet.");
  } else {
    setText(elements.sessionListState, "Select a session or remove its local history.");
  }

  for (const session of sessions) {
    elements.sessionList.append(createSessionListItem(session, selectedSessionId));
  }
}

function renderSession(elements, controller, options) {
  const id = stringOrEmpty(options.id);
  const title = id ? textOr(options.title, "Untitled session") : "No session selected";
  const status = normalizeStatus(options.status);

  setText(elements.sessionTitle, title);
  setText(elements.sessionId, id || "—");
  updateCopyButton(elements.copySessionId, "Session ID", id);
  setStatusBadge(elements.sessionStatus, status, { hidden: !id || !options.status });

  if (options.composer) {
    controller.setComposer(options.composer);
  }
}

function renderRequests(shell, elements, state, controller, options) {
  const requests = Array.isArray(options.requests) ? options.requests.filter(hasId) : [];
  const requestedState = ["loading", "error", "empty", "ready"].includes(options.state)
    ? options.state
    : "";
  const renderState = requestedState === "loading" || requestedState === "error"
    ? requestedState
    : requests.length > 0 ? "ready" : "empty";

  state.requests.clear();
  clear(elements.requestList);
  elements.transcript.setAttribute("aria-busy", String(renderState === "loading"));
  elements.conversationLoading.hidden = renderState !== "loading";
  elements.conversationError.hidden = renderState !== "error";
  elements.emptyState.hidden = renderState !== "empty";

  if (renderState === "error") {
    setText(elements.conversationErrorText, textOr(options.error, "The conversation could not be loaded."));
  }

  for (const request of requests) {
    const id = String(request.id);
    state.requests.set(id, request);
    elements.requestList.append(createRequestElement(request, id === String(options.selectedRequestId ?? state.selectedRequestId ?? "")));
  }

  const requestedSelection = stringOrEmpty(options.selectedRequestId);
  const selectedRequestId = state.requests.has(requestedSelection)
    ? requestedSelection
    : state.requests.has(state.selectedRequestId)
      ? state.selectedRequestId
      : "";

  if (selectedRequestId) {
    controller.selectRequest(selectedRequestId, {
      focus: false,
      reveal: options.revealSelected === true,
    });
  } else {
    state.selectedRequestId = null;
    controller.renderInspector(null);
  }
}

function selectRequest(shell, elements, state, controller, requestId, options) {
  const id = stringOrEmpty(requestId);
  const request = state.requests.get(id);
  if (!request) {
    return false;
  }

  const changed = state.selectedRequestId !== id;
  state.selectedRequestId = id;
  for (const card of shell.querySelectorAll("[data-request-card]")) {
    const selected = card.dataset.requestId === id;
    card.classList.toggle("durable-chat-request--selected", selected);
    card.setAttribute("aria-current", selected ? "true" : "false");
  }

  controller.renderInspector(request);
  if (options.reveal !== false) {
    controller.setDetailsVisible(true);
  }

  if (options.focus === true) {
    elements.detailsHeading.focus({ preventScroll: true });
  }

  if (changed || options.notify === true) {
    emit(shell, "durable-chat:request-selected", { requestId: id });
  }

  return true;
}

function renderInspector(elements, request) {
  if (!request) {
    setText(elements.detailsSelection, "Select a request to inspect its durable context.");
    setText(elements.runId, "—");
    setText(elements.sandboxGroupLabel, "Sandbox Group");
    setText(elements.sandboxGroup, "—");
    setText(elements.sandboxId, "—");
    setText(elements.progressSummary, "No request selected.");
    setStatusBadge(elements.detailsStatus, "unknown", { hidden: true });
    updateCopyButton(elements.copyRunId, "Run ID", "");
    updateCopyButton(elements.copySandboxId, "Sandbox ID", "");
    updateCopyButton(elements.copySandboxGroup, "Sandbox Group", "");
    renderHistory(elements.sandboxHistory, []);
    renderProgress(elements.lifecycleProgress, []);
    setDiagnosticLink(elements.openDts, "");
    setDiagnosticLink(elements.openAppInsights, "");
    setText(elements.diagnosticsNote, "Select a request to view available diagnostic links.");
    return;
  }

  const sandbox = isObject(request.sandbox) ? request.sandbox : {};
  const diagnostics = isObject(request.diagnostics) ? request.diagnostics : {};
  const progress = Array.isArray(request.progress) ? request.progress : [];
  const runId = stringOrEmpty(request.runId);
  const sandboxGroup = textOr(sandbox.group ?? request.sandboxGroup, "Not reported");
  const sandboxGroupLabel = sandbox.groupSource === "configured"
    ? "Configured Sandbox Group"
    : "Sandbox Group";
  const sandboxId = stringOrEmpty(sandbox.id);

  setText(elements.detailsSelection, "Details for the selected request.");
  setText(elements.runId, runId || "Not reported");
  setText(elements.sandboxGroupLabel, sandboxGroupLabel);
  setText(elements.sandboxGroup, sandboxGroup);
  setText(elements.sandboxId, sandboxId || "Not reported");
  setStatusBadge(elements.detailsStatus, normalizeStatus(request.status));
  updateCopyButton(elements.copyRunId, "Run ID", runId);
  updateCopyButton(elements.copySandboxId, "Sandbox ID", sandboxId);
  updateCopyButton(
    elements.copySandboxGroup,
    sandboxGroupLabel,
    sandboxGroup === "Not reported" ? "" : sandboxGroup,
  );
  renderHistory(elements.sandboxHistory, Array.isArray(sandbox.history) ? sandbox.history : []);
  renderProgress(elements.lifecycleProgress, progress);
  setText(
    elements.progressSummary,
    progress.length === 1
      ? "1 lifecycle update reported."
      : progress.length > 1
        ? `${progress.length} lifecycle updates reported.`
        : "No lifecycle updates have been reported.",
  );

  const dtsAvailable = setDiagnosticLink(elements.openDts, diagnostics.dtsUrl);
  const appInsightsAvailable = setDiagnosticLink(elements.openAppInsights, diagnostics.appInsightsUrl);
  const diagnosticNote = textOr(
    diagnostics.note,
    [!dtsAvailable ? textOr(diagnostics.dtsReason, "DTS is not available for this request.") : "", !appInsightsAvailable ? textOr(diagnostics.appInsightsReason, "Application Insights is not available for this request.") : ""]
      .filter(Boolean)
      .join(" "),
  );
  setText(elements.diagnosticsNote, diagnosticNote || "Request diagnostic links are available.");
}

function setComposer(elements, options) {
  const enabled = options.enabled === true;
  const cancelable = options.cancelable === true;
  const requestId = stringOrEmpty(options.requestId);

  elements.prompt.disabled = !enabled;
  elements.send.disabled = !enabled;
  elements.cancelRequest.disabled = !cancelable;
  elements.cancelRequest.dataset.requestId = requestId;

  if (typeof options.placeholder === "string") {
    elements.prompt.placeholder = options.placeholder;
  } else if (enabled) {
    elements.prompt.placeholder = "Ask your agent to do something…";
  } else {
    elements.prompt.placeholder = "Choose a session to begin.";
  }

  setText(
    elements.composerState,
    textOr(options.message, enabled ? "Durable execution sends one request at a time." : "Choose or create a session to compose a request."),
  );
}

function setSandboxProfiles(elements, options) {
  const profiles = Array.isArray(options.profiles)
    ? options.profiles.filter((profile) => isObject(profile) && stringOrEmpty(profile.value))
    : [];
  const selectedProfile = stringOrEmpty(options.selectedProfile);

  clear(elements.sandboxProfile);
  if (profiles.length === 0) {
    const option = document.createElement("option");
    option.value = "";
    setText(option, "Sandbox unavailable");
    elements.sandboxProfile.append(option);
    elements.sandboxProfile.disabled = true;
    return;
  }

  for (const profile of profiles) {
    const option = document.createElement("option");
    option.value = profile.value;
    setText(option, textOr(profile.label, profile.value));
    option.selected = profile.value === selectedProfile;
    elements.sandboxProfile.append(option);
  }

  if (!elements.sandboxProfile.value && profiles[0]) {
    elements.sandboxProfile.value = profiles[0].value;
  }
  elements.sandboxProfile.disabled = options.enabled !== true;
}

function setStorageNotice(elements, options) {
  const visible = options.visible === true;
  elements.storageNotice.hidden = !visible;
  if (visible) {
    replaceWithParagraph(
      elements.storageNotice,
      textOr(options.message, "Browser storage is unavailable. This page is using volatile history."),
    );
  }
}

function renderHistoryStatus(elements, historyMode) {
  const mode = historyMode === "persistent" || historyMode === "volatile"
    ? historyMode
    : "";
  const message = mode === "persistent"
    ? "Session history is saved in this browser."
    : mode === "volatile"
      ? "Browser storage is unavailable. This session history will be lost when this page closes."
      : "";
  elements.historyStatus.hidden = !message;
  elements.historyStatus.dataset.storageMode = mode;
  setText(elements.historyStatusText, message);
}

function setDetailsVisible(shell, elements, visible, options = {}) {
  const persist = options.persist !== false;
  shell.dataset.detailsVisible = String(visible);
  elements.detailsPanel.hidden = !visible;
  elements.detailsToggle.setAttribute("aria-expanded", String(visible));
  elements.detailsToggle.setAttribute("aria-label", visible ? "Hide request details" : "Show request details");
  setText(elements.detailsToggleLabel, visible ? "Hide details" : "Show details");

  if (persist) {
    writeDetailsPreference(visible);
  }

  emit(shell, "durable-chat:details-visibility-change", { visible });
}

function setSidebarOpen(shell, elements, open) {
  shell.dataset.sidebarOpen = String(open);
  elements.sidebarToggle.setAttribute("aria-expanded", String(open));
  elements.sidebarToggle.setAttribute("aria-label", open ? "Hide sessions" : "Show sessions");
  elements.sidebarScrim.hidden = !open;
}

function createSessionListItem(session, selectedSessionId) {
  const sessionId = String(session.id);
  const item = document.createElement("div");
  item.className = "durable-chat-session-list__item";
  item.dataset.sessionId = sessionId;

  const select = document.createElement("button");
  select.className = "durable-chat-session-select";
  select.type = "button";
  select.dataset.action = "select-session";
  select.dataset.sessionId = sessionId;
  select.setAttribute("aria-current", String(sessionId === selectedSessionId ? "page" : "false"));

  const icon = createIcon("durable-chat-icon-chat");
  const copy = document.createElement("span");
  copy.className = "durable-chat-session-select__copy";
  const title = document.createElement("strong");
  title.dataset.sessionTitle = "true";
  const meta = document.createElement("small");
  setText(title, textOr(session.title, "Untitled session"));
  setText(meta, session.status ? statusLabel(session.status) : "No requests yet");
  copy.append(title, meta);
  select.append(icon, copy);
  item.append(select);

  if (session.removable !== false) {
    const rename = document.createElement("button");
    rename.className = "durable-chat-icon-button durable-chat-session-rename";
    rename.type = "button";
    rename.dataset.action = "rename-session";
    rename.dataset.sessionId = sessionId;
    rename.setAttribute("aria-label", `Rename ${textOr(session.title, "this session")}`);
    rename.title = "Rename session";
    rename.append(createIcon("durable-chat-icon-edit"));
    item.append(rename);

    const remove = document.createElement("button");
    remove.className = "durable-chat-icon-button durable-chat-session-remove";
    remove.type = "button";
    remove.dataset.action = "remove-session";
    remove.dataset.sessionId = sessionId;
    remove.setAttribute("aria-label", `Remove local history for ${textOr(session.title, "this session")}`);
    remove.title = "Remove local history";
    remove.append(createIcon("durable-chat-icon-close"));
    item.append(remove);
  }

  return item;
}

function beginSessionRename(shell, elements, sessionId) {
  const item = shell.querySelector(
    `.durable-chat-session-list__item[data-session-id="${cssEscape(sessionId)}"]`,
  );
  const title = item?.querySelector("[data-session-title]");
  if (!(title instanceof HTMLElement) || item.querySelector("[data-session-rename-form]")) {
    return;
  }

  const form = document.createElement("form");
  form.className = "durable-chat-session-rename-form";
  form.dataset.sessionRenameForm = "true";
  form.dataset.sessionId = sessionId;
  const input = document.createElement("input");
  input.name = "title";
  input.type = "text";
  input.maxLength = 160;
  input.value = title.textContent ?? "";
  input.setAttribute("aria-label", "Session name");
  const save = document.createElement("button");
  save.className = "durable-chat-text-button";
  save.type = "submit";
  setText(save, "Save");
  const cancel = document.createElement("button");
  cancel.className = "durable-chat-text-button";
  cancel.type = "button";
  cancel.dataset.action = "cancel-session-rename";
  cancel.dataset.sessionId = sessionId;
  setText(cancel, "Cancel");
  form.append(input, save, cancel);
  title.replaceWith(form);
  input.focus();
  input.select();
}

function cancelSessionRename(shell, sessionId) {
  const item = shell.querySelector(
    `.durable-chat-session-list__item[data-session-id="${cssEscape(sessionId)}"]`,
  );
  const form = item?.querySelector("[data-session-rename-form]");
  if (!(form instanceof HTMLFormElement)) {
    return;
  }
  const title = document.createElement("strong");
  title.dataset.sessionTitle = "true";
  setText(title, form.elements.title?.value || "Untitled session");
  form.replaceWith(title);
}

function createRequestElement(request, isSelected) {
  const requestId = String(request.id);
  const requestElement = document.createElement("section");
  requestElement.className = "durable-chat-request";
  requestElement.dataset.requestCard = "true";
  requestElement.dataset.requestId = requestId;
  requestElement.setAttribute("aria-current", String(isSelected));
  if (isSelected) {
    requestElement.classList.add("durable-chat-request--selected");
  }

  const divider = document.createElement("div");
  divider.className = "durable-chat-request-divider";
  setText(divider, "Request");
  requestElement.append(divider);

  if (stringOrEmpty(request.prompt)) {
    requestElement.append(createMessage("user", "You", request.prompt));
  }

  const isDraft = request.response?.draft === true;
  const assistantMessage = createMessage("assistant", isDraft ? "Agent draft" : "Agent", responseText(request), {
    id: `${requestDomId(requestId)}-response`,
    placeholder: responsePlaceholder(request),
  });
  assistantMessage.classList.toggle("durable-chat-message--draft", isDraft);
  const progress = Array.isArray(request.progress) ? request.progress : [];
  assistantMessage.querySelector("[data-response-content]").after(createInlineProgress(progress, requestId));

  const responseMode = responseModeText(request);
  if (responseMode) {
    const mode = document.createElement("span");
    mode.className = "durable-chat-response-mode";
    mode.append(createStatusDot(), document.createTextNode(responseMode));
    assistantMessage.querySelector(".durable-chat-message__content").append(mode);
  }

  requestElement.append(assistantMessage);

  if (request.error?.message) {
    const error = document.createElement("p");
    error.className = "durable-chat-error-note";
    setText(error, request.error.message);
    requestElement.append(error);
  }

  if (isObject(request.humanInput)) {
    requestElement.append(createHumanInput(request.humanInput, requestId));
  }

  const actions = document.createElement("div");
  actions.className = "durable-chat-request-actions";

  const details = document.createElement("button");
  details.className = "durable-chat-request-action";
  details.type = "button";
  details.dataset.action = "view-details";
  details.dataset.requestId = requestId;
  setText(details, "View details");
  actions.append(details);

  if (request.canCancel === true) {
    const cancel = document.createElement("button");
    cancel.className = "durable-chat-request-action durable-chat-request-action--cancel";
    cancel.type = "button";
    cancel.dataset.action = "cancel-request";
    cancel.dataset.requestId = requestId;
    setText(cancel, "Cancel");
    actions.append(cancel);
  }

  actions.append(createStatusBadge(normalizeStatus(request.status)));
  requestElement.append(actions);
  return requestElement;
}

function createMessage(kind, label, body, options = {}) {
  const message = document.createElement("article");
  message.className = `durable-chat-message durable-chat-message--${kind}`;

  const avatar = document.createElement("span");
  avatar.className = "durable-chat-message__avatar";
  if (kind === "assistant") {
    avatar.append(createIcon("durable-chat-icon-box"));
  } else {
    setText(avatar, "You");
  }

  const content = document.createElement("div");
  content.className = "durable-chat-message__content";
  const messageLabel = document.createElement("div");
  messageLabel.className = "durable-chat-message__label";
  setText(messageLabel, label);

  const messageBody = document.createElement("p");
  messageBody.className = "durable-chat-message__body";
  messageBody.dataset.responseContent = kind === "assistant" ? "true" : "false";
  if (kind === "assistant") {
    messageBody.dataset.requestSlot = "response";
  }
  if (options.id) {
    messageBody.id = options.id;
  }

  if (body) {
    setText(messageBody, body);
  } else {
    messageBody.classList.add("durable-chat-response-placeholder");
    setText(messageBody, options.placeholder || "Response content is not available.");
  }

  content.append(messageLabel, messageBody);
  message.append(avatar, content);
  return message;
}

function createInlineProgress(progress, requestId) {
  const progressList = document.createElement("ul");
  progressList.className = "durable-chat-progress-inline";
  progressList.id = `${requestDomId(requestId)}-progress`;
  progressList.dataset.requestProgress = "true";
  progressList.dataset.requestSlot = "progress";
  progressList.setAttribute("aria-label", "Request progress");

  for (const step of progress) {
    const item = document.createElement("li");
    item.dataset.progressState = normalizeProgressState(step?.state);
    setText(item, textOr(step?.label, "Progress update"));
    progressList.append(item);
  }

  return progressList;
}

function createHumanInput(input, requestId) {
  const inputId = stringOrEmpty(input.id) || `${requestDomId(requestId)}-input`;
  const section = document.createElement("section");
  section.className = "durable-chat-human-input";
  section.id = `${requestDomId(requestId)}-human-input`;
  section.dataset.requestSlot = "human-input";
  section.setAttribute("aria-label", "Input required");
  section.setAttribute("aria-live", "polite");

  const heading = document.createElement("div");
  heading.className = "durable-chat-human-input__heading";
  heading.append(createIcon("durable-chat-icon-warning"));
  const headingText = document.createElement("h4");
  setText(headingText, "Input required");
  heading.append(headingText);
  section.append(heading);

  if (stringOrEmpty(input.prompt)) {
    const prompt = document.createElement("p");
    setText(prompt, input.prompt);
    section.append(prompt);
  }

  const choices = Array.isArray(input.choices) ? input.choices : [];
  if (choices.length > 0) {
    const choiceList = document.createElement("div");
    choiceList.className = "durable-chat-human-input__choices";
    for (const choice of choices) {
      const value = stringOrEmpty(isObject(choice) ? choice.value : choice);
      if (!value) {
        continue;
      }
      const button = document.createElement("button");
      button.className = "durable-chat-human-input__choice";
      button.type = "button";
      button.dataset.action = "human-choice";
      button.dataset.requestId = requestId;
      button.dataset.humanInputId = inputId;
      button.dataset.answer = value;
      button.disabled = input.submitting === true;
      setText(button, textOr(isObject(choice) ? choice.label : "", value));
      choiceList.append(button);
    }
    if (choiceList.childElementCount > 0) {
      section.append(choiceList);
    }
  }

  if (input.allowFreeText === true || isObject(input.schema)) {
    const form = document.createElement("form");
    form.dataset.humanInputForm = "true";
    form.dataset.requestId = requestId;
    form.dataset.humanInputId = inputId;

    const label = document.createElement("label");
    const responseId = `${requestDomId(requestId)}-human-response`;
    label.htmlFor = responseId;
    setText(label, textOr(input.schema?.label, "Response"));
    form.append(label);

    if (isObject(input.schema) && stringOrEmpty(input.schema.description)) {
      const description = document.createElement("p");
      setText(description, input.schema.description);
      form.append(description);
    }

    if (isObject(input.schema) && stringOrEmpty(input.schema.value)) {
      const details = document.createElement("details");
      const summary = document.createElement("summary");
      setText(summary, "Response schema");
      const schema = document.createElement("pre");
      setText(schema, input.schema.value);
      details.append(summary, schema);
      form.append(details);
    }

    const response = document.createElement("textarea");
    response.id = responseId;
    response.name = "response";
    response.rows = 3;
    response.required = true;
    response.disabled = input.submitting === true;
    response.placeholder = isObject(input.schema)
      ? "Provide a response that matches the requested schema."
      : "Enter your response.";
    form.append(response);

    const submit = document.createElement("button");
    submit.className = "durable-chat-human-input__submit";
    submit.type = "submit";
    submit.disabled = input.submitting === true;
    setText(submit, input.submitting === true ? "Submitting response…" : "Submit response");
    form.append(submit);
    section.append(form);
  }

  return section;
}

function renderHistory(container, history) {
  clear(container);
  if (history.length === 0) {
    const item = document.createElement("li");
    setText(item, "No sandbox history has been recorded.");
    container.append(item);
    return;
  }

  for (const entry of history) {
    const item = document.createElement("li");
    if (isObject(entry)) {
      setText(item, [textOr(entry.label, ""), textOr(entry.detail, "")].filter(Boolean).join(" — "));
    } else {
      setText(item, entry);
    }
    container.append(item);
  }
}

function renderProgress(container, progress) {
  clear(container);
  if (progress.length === 0) {
    const item = document.createElement("li");
    item.className = "durable-chat-progress-list__empty";
    setText(item, "No lifecycle updates have been reported.");
    container.append(item);
    return;
  }

  for (const step of progress) {
    const item = document.createElement("li");
    const progressState = normalizeProgressState(step?.state);
    const icon = document.createElement("span");
    icon.className = "durable-chat-progress-list__icon";
    icon.dataset.progressState = progressState;
    if (progressState === "completed") {
      icon.append(createIcon("durable-chat-icon-check"));
    } else if (progressState === "waiting") {
      icon.append(createIcon("durable-chat-icon-warning"));
    } else if (progressState === "failed") {
      icon.append(createIcon("durable-chat-icon-close"));
    } else if (progressState === "running") {
      icon.append(createIcon("durable-chat-icon-clock"));
    }

    const copy = document.createElement("div");
    copy.className = "durable-chat-progress-list__copy";
    const label = document.createElement("strong");
    setText(label, textOr(step?.label, "Progress update"));
    copy.append(label);
    if (stringOrEmpty(step?.detail)) {
      const detail = document.createElement("span");
      setText(detail, step.detail);
      copy.append(detail);
    }

    item.append(icon, copy);
    if (stringOrEmpty(step?.timestamp)) {
      const timestamp = document.createElement("time");
      setText(timestamp, step.timestamp);
      item.append(timestamp);
    }
    container.append(item);
  }
}

function setStatusBadge(element, status, options = {}) {
  const normalized = normalizeStatus(status);
  element.hidden = options.hidden === true;
  element.className = `durable-chat-status-badge durable-chat-status--${normalized}`;
  setText(element, statusLabel(normalized));
}

function createStatusBadge(status) {
  const badge = document.createElement("span");
  setStatusBadge(badge, status);
  return badge;
}

function setDiagnosticLink(link, value) {
  const href = safeHttpsUrl(value);
  if (!href) {
    link.removeAttribute("href");
    link.removeAttribute("target");
    link.removeAttribute("rel");
    link.setAttribute("aria-disabled", "true");
    link.tabIndex = -1;
    return false;
  }

  link.href = href;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  link.setAttribute("aria-disabled", "false");
  link.removeAttribute("tabindex");
  return true;
}

function submitComposer(shell, elements) {
  if (elements.prompt.disabled) {
    return;
  }

  const message = elements.prompt.value;
  if (!message.trim()) {
    setText(elements.announcements, "Enter a message before sending.");
    return;
  }

  emit(shell, "durable-chat:submit-message", { message });
}

function submitHumanInput(shell, form, elements) {
  const response = form.elements.namedItem("response");
  const value = response instanceof HTMLTextAreaElement ? response.value : "";
  if (!value.trim()) {
    setText(elements.announcements, "Enter a response before submitting.");
    return;
  }

  emit(shell, "durable-chat:respond-human-input", {
    inputId: form.dataset.humanInputId,
    requestId: form.dataset.requestId,
    response: value,
    responseType: "text",
  });
}

function copyIdentifier(shell, elements, button) {
  const value = stringOrEmpty(button.dataset.copyValue);
  const label = textOr(button.dataset.copyLabel, "Identifier");
  if (!value) {
    return;
  }

  emit(shell, "durable-chat:copy-identifier", { label, value });
  if (navigator.clipboard?.writeText) {
    navigator.clipboard.writeText(value)
      .then(() => setText(elements.announcements, `${label} copied.`))
      .catch(() => selectIdentifier(button.dataset.copyTarget, elements.announcements, label));
    return;
  }

  selectIdentifier(button.dataset.copyTarget, elements.announcements, label);
}

function selectIdentifier(targetId, announcements, label) {
  const target = document.getElementById(targetId);
  const selection = window.getSelection();
  if (target && selection) {
    const range = document.createRange();
    range.selectNodeContents(target);
    selection.removeAllRanges();
    selection.addRange(range);
    setText(announcements, `${label} selected. Copy it with your browser command.`);
    return;
  }

  setText(announcements, `${label} could not be copied.`);
}

function updateCopyButton(button, label, value) {
  button.disabled = !value;
  button.dataset.copyLabel = label;
  button.dataset.copyValue = value;
}

function responseText(request) {
  if (typeof request.response === "string") {
    return request.response;
  }

  return stringOrEmpty(request.response?.text);
}

function responsePlaceholder(request) {
  const status = normalizeStatus(request.status);
  if (status === "failed" || status === "expired" || status === "session_busy") {
    return "No response was returned.";
  }
  if (status === "uncertain") {
    return "The request may have started. Reconciliation is in progress.";
  }
  if (request.completionPending === true) {
    return "The final response is being retrieved.";
  }
  if (request.response?.mode === "background") {
    return "This background response will be reported when the run completes.";
  }
  return "Response content has not arrived.";
}

function responseModeText(request) {
  if (request.response?.draft === true) {
    return request.response.mode === "foreground" ? "Streaming draft" : "Draft response";
  }
  if (request.response?.mode === "foreground" && normalizeStatus(request.status) === "running") {
    return "Receiving response";
  }
  if (request.response?.mode === "background") {
    return "Background response";
  }
  return "";
}

function normalizeStatus(value) {
  return Object.prototype.hasOwnProperty.call(STATUS_LABELS, value) ? value : "unknown";
}

function statusLabel(status) {
  return STATUS_LABELS[normalizeStatus(status)];
}

function normalizeProgressState(value) {
  return PROGRESS_STATES.has(value) ? value : "pending";
}

function safeHttpsUrl(value) {
  if (!stringOrEmpty(value)) {
    return "";
  }

  try {
    const url = new URL(value);
    const localhostHttp = url.protocol === "http:"
      && ["localhost", "127.0.0.1", "[::1]", "::1"].includes(url.hostname);
    if ((url.protocol !== "https:" && !localhostHttp) || url.username || url.password) {
      return "";
    }
    if (hasSensitiveDiagnosticUrlMaterial(url)) {
      return "";
    }
    return url.href;
  } catch {
    return "";
  }
}

function hasSensitiveDiagnosticUrlMaterial(url) {
  const fragment = url.hash.startsWith("#") ? url.hash.slice(1) : url.hash;
  const fragmentQuery = fragment.includes("?")
    ? fragment.slice(fragment.indexOf("?") + 1)
    : fragment;
  for (const component of [url.search, fragmentQuery]) {
    for (const [key, queryValue] of new URLSearchParams(component)) {
      const normalized = key.replaceAll("-", "").replaceAll("_", "").toLowerCase();
      if (
        SENSITIVE_DIAGNOSTIC_QUERY_KEYS.has(key.toLowerCase())
        || SENSITIVE_DIAGNOSTIC_QUERY_KEYS.has(normalized)
        || /(?:accountkey|sharedaccesssignature|sig|token|x-functions-key)=/i.test(queryValue)
      ) {
        return true;
      }
    }
  }
  return false;
}

function cssEscape(value) {
  return globalThis.CSS?.escape
    ? globalThis.CSS.escape(value)
    : value.replace(/["\\]/g, "\\$&");
}

function createIcon(symbolId) {
  const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  icon.setAttribute("class", "durable-chat-icon");
  icon.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#${symbolId}`);
  icon.append(use);
  return icon;
}

function createStatusDot() {
  const dot = document.createElement("span");
  dot.className = "durable-chat-status-dot";
  dot.setAttribute("aria-hidden", "true");
  return dot;
}

function requestDomId(requestId) {
  const bytes = new TextEncoder().encode(requestId);
  return `durable-chat-request-${Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("")}`;
}

function emit(shell, name, detail = {}) {
  shell.dispatchEvent(new CustomEvent(name, { bubbles: true, detail }));
}

function readDetailsPreference() {
  try {
    return window.localStorage.getItem(DETAILS_PREFERENCE_KEY) !== "false";
  } catch {
    return true;
  }
}

function writeDetailsPreference(visible) {
  try {
    window.localStorage.setItem(DETAILS_PREFERENCE_KEY, String(visible));
  } catch {
    // The shell remains usable when browser storage is blocked or full.
  }
}

function addMediaListener(mediaQuery, listener) {
  if (typeof mediaQuery.addEventListener === "function") {
    mediaQuery.addEventListener("change", listener);
  } else {
    mediaQuery.addListener(listener);
  }
}

function removeMediaListener(mediaQuery, listener) {
  if (!listener) {
    return;
  }
  if (typeof mediaQuery.removeEventListener === "function") {
    mediaQuery.removeEventListener("change", listener);
  } else {
    mediaQuery.removeListener(listener);
  }
}

function replaceWithParagraph(container, value) {
  const paragraph = document.createElement("p");
  setText(paragraph, value);
  container.replaceChildren(paragraph);
}

function clear(element) {
  element.replaceChildren();
}

function setText(element, value) {
  element.textContent = String(value ?? "");
}

function textOr(value, fallback) {
  return stringOrEmpty(value) || fallback;
}

function stringOrEmpty(value) {
  return typeof value === "string" ? value : "";
}

function hasId(value) {
  return isObject(value) && stringOrEmpty(value.id);
}

function isObject(value) {
  return value !== null && typeof value === "object";
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", () => {
    createDurableChatShell();
  }, { once: true });
} else {
  createDurableChatShell();
}
