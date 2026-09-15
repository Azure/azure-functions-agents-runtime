import { createDurableChatShell } from "./rendering.js";
import { DurableChatHistoryError, openDurableChatHistory } from "./history.js";

const ROUTE_CONTRACT = Object.freeze({
  start_run: { method: "POST", parameters: [] },
  status: { method: "GET", parameters: ["run_id"] },
  result: { method: "GET", parameters: ["run_id"] },
  cancel: { method: "POST", parameters: ["run_id"] },
  human_input_detail: { method: "GET", parameters: ["run_id", "request_id"] },
  human_input_submit: { method: "POST", parameters: ["run_id", "request_id"] },
  events: { method: "GET", parameters: ["run_id"] },
  diagnostics: { method: "GET", parameters: ["run_id"] },
});

const ACTIVE_UI_STATUSES = new Set([
  "submitting",
  "queued",
  "running",
  "waiting",
  "uncertain",
  "reconnecting",
  "degraded",
  "cancel_requested",
]);
const MAX_START_ATTEMPTS = 2;
const MAX_STREAM_FAILURES = 3;
const MAX_PROMPT_UTF8_BYTES = 256 * 1024;
const STATUS_POLL_MILLISECONDS = 4_000;
const EVENT_RECONNECT_MILLISECONDS = 500;
const ROUTE_PARAMETER_PATTERN = /\{([a-z_]+)\}/g;
const AGENT_SLUG_PATTERN = /^[A-Za-z_][A-Za-z0-9_.-]{0,127}$/;
const HISTORY_NAMESPACE_PATTERN = /^[0-9a-f]{64}$/;
const SELECTED_REQUEST_STORAGE_PREFIX = "azure-functions-agents.durable-chat.selected-request.";
const SENSITIVE_URL_KEYS = new Set([
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

export class DurableChatHttpError extends Error {
  constructor(status, payload, message = "The Function App request failed.") {
    super(message);
    this.name = "DurableChatHttpError";
    this.status = status;
    this.payload = isObject(payload) ? payload : {};
  }
}

export class DurableChatProtocolError extends Error {
  constructor(message) {
    super(message);
    this.name = "DurableChatProtocolError";
  }
}

/**
 * Decode UTF-8 SSE data incrementally. The parser accepts network chunks, so
 * a multi-byte character or SSE field may be split at any byte boundary.
 */
export class DurableChatSseParser {
  constructor() {
    this._decoder = new TextDecoder("utf-8", { fatal: true });
    this._buffer = "";
    this._event = emptySseEvent();
  }

  push(chunk) {
    if (!(chunk instanceof Uint8Array)) {
      throw new DurableChatProtocolError("SSE input must be a byte chunk.");
    }
    try {
      this._buffer += this._decoder.decode(chunk, { stream: true });
    } catch {
      throw new DurableChatProtocolError("The event stream is not valid UTF-8.");
    }
    return this._drainLines(false);
  }

  finish() {
    try {
      this._buffer += this._decoder.decode();
    } catch {
      throw new DurableChatProtocolError("The event stream is not valid UTF-8.");
    }
    const frames = this._drainLines(true);
    if (hasSseEventData(this._event)) {
      frames.push(this._emitEvent());
    }
    return frames;
  }

  _drainLines(finished) {
    const frames = [];
    while (true) {
      const newline = this._buffer.indexOf("\n");
      if (newline < 0) {
        break;
      }
      let line = this._buffer.slice(0, newline);
      this._buffer = this._buffer.slice(newline + 1);
      if (line.endsWith("\r")) {
        line = line.slice(0, -1);
      }
      this._consumeLine(line, frames);
    }
    if (finished && this._buffer) {
      let line = this._buffer;
      this._buffer = "";
      if (line.endsWith("\r")) {
        line = line.slice(0, -1);
      }
      this._consumeLine(line, frames);
    }
    return frames;
  }

  _consumeLine(line, frames) {
    if (line === "") {
      if (hasSseEventData(this._event)) {
        frames.push(this._emitEvent());
      }
      return;
    }
    if (line.startsWith(":")) {
      return;
    }

    const separator = line.indexOf(":");
    const field = separator < 0 ? line : line.slice(0, separator);
    let value = separator < 0 ? "" : line.slice(separator + 1);
    if (value.startsWith(" ")) {
      value = value.slice(1);
    }
    if (field === "data") {
      this._event.data.push(value);
    } else if (field === "event") {
      this._event.event = value;
    } else if (field === "id" && !value.includes("\0")) {
      this._event.id = value;
    }
  }

  _emitEvent() {
    const event = {
      event: this._event.event,
      id: this._event.id,
      data: this._event.data.join("\n"),
    };
    this._event = emptySseEvent();
    return event;
  }
}

/**
 * Return the route-rooted shell URL for either a default or custom Functions
 * host prefix. Static hosting redirects the no-slash route, but this remains
 * correct if a page is restored before that redirect completes.
 */
export function deriveDurableChatShellBase(locationLike = globalThis.location) {
  const page = asHttpLocation(locationLike);
  const marker = "/experimental/durable-chat";
  const markerIndex = page.pathname.indexOf(marker);
  if (
    markerIndex >= 0
    && (page.pathname.length === markerIndex + marker.length
      || page.pathname[markerIndex + marker.length] === "/")
  ) {
    const shellPath = `${page.pathname.slice(0, markerIndex + marker.length)}/`;
    return new URL(shellPath, page.origin).href;
  }
  return new URL("./", page).href;
}

/**
 * Validate the authenticated bootstrap without trusting it to choose another
 * origin or another deployment path for requests carrying browser credentials.
 */
export function normalizeDurableChatBootstrap(payload, locationLike = globalThis.location) {
  if (!isObject(payload) || payload.schema_version !== "1") {
    throw new DurableChatProtocolError("The Durable Chat bootstrap is invalid.");
  }
  if (
    !isObject(payload.agent)
    || payload.agent.schema_version !== "1"
    || !AGENT_SLUG_PATTERN.test(payload.agent.slug)
    || !validDisplayName(payload.agent.display_name)
  ) {
    throw new DurableChatProtocolError("The Durable Chat agent identity is invalid.");
  }
  if (!HISTORY_NAMESPACE_PATTERN.test(payload.history_namespace)) {
    throw new DurableChatProtocolError("The Durable Chat history namespace is invalid.");
  }
  if (
    payload.sandbox_group_resource_id !== undefined
    && payload.sandbox_group_resource_id !== null
    && !nonEmptyString(payload.sandbox_group_resource_id)
  ) {
    throw new DurableChatProtocolError("The Durable Chat configured sandbox group is invalid.");
  }
  if (!Array.isArray(payload.supported_sandbox_profiles) || payload.supported_sandbox_profiles.length === 0) {
    throw new DurableChatProtocolError("The Durable Chat sandbox profiles are invalid.");
  }

  const profiles = payload.supported_sandbox_profiles.filter(isSandboxProfile);
  if (
    profiles.length !== payload.supported_sandbox_profiles.length
    || new Set(profiles).size !== profiles.length
    || !profiles.includes(payload.default_sandbox_profile)
    || typeof payload.foreground_streaming_available !== "boolean"
  ) {
    throw new DurableChatProtocolError("The Durable Chat bootstrap options are invalid.");
  }

  const page = asHttpLocation(locationLike);
  const deploymentRoot = deploymentRootFor(page);
  const routes = new Map();
  if (!Array.isArray(payload.routes) || payload.routes.length !== Object.keys(ROUTE_CONTRACT).length) {
    throw new DurableChatProtocolError("The Durable Chat route list is invalid.");
  }
  for (const descriptor of payload.routes) {
    if (
      !isObject(descriptor)
      || descriptor.schema_version !== "1"
      || typeof descriptor.name !== "string"
    ) {
      throw new DurableChatProtocolError("The Durable Chat route descriptor is invalid.");
    }
    const expected = ROUTE_CONTRACT[descriptor.name];
    if (!expected || descriptor.method !== expected.method || routes.has(descriptor.name)) {
      throw new DurableChatProtocolError("The Durable Chat route descriptor is invalid.");
    }
    const pathTemplate = validateRouteTemplate(
      descriptor.path_template,
      expected.parameters,
      page,
      deploymentRoot,
    );
    routes.set(descriptor.name, { method: descriptor.method, pathTemplate });
  }
  if (routes.size !== Object.keys(ROUTE_CONTRACT).length) {
    throw new DurableChatProtocolError("The Durable Chat bootstrap is missing a route.");
  }

  return {
    agent: {
      slug: payload.agent.slug,
      displayName: payload.agent.display_name,
    },
    defaultSandboxProfile: payload.default_sandbox_profile,
    foregroundStreamingAvailable: payload.foreground_streaming_available,
    historyNamespace: payload.history_namespace,
    integrations: isObject(payload.integrations) ? clone(payload.integrations) : null,
    routes,
    sandboxGroupResourceId: stringOr(payload.sandbox_group_resource_id, ""),
    supportedSandboxProfiles: profiles,
  };
}

/**
 * Apply one server observation to a client projection. It deliberately does
 * not mark an event-terminal result as committed: polling the result route
 * retains that authority.
 */
export function reduceDurableChatProjection(current, event, options = {}) {
  const next = clone(isObject(current) ? current : {});
  if (!isObject(event) || typeof event.event_type !== "string") {
    throw new DurableChatProtocolError("The Durable Chat event is invalid.");
  }

  next.lastObservedAt = stringOr(event.observed_at, next.lastObservedAt);
  switch (event.event_type) {
    case "run_status":
      next.status = uiStatus(event.status);
      next.phase = stringOr(event.phase, next.phase);
      next.resultAvailable = event.result_available === true;
      if (next.status !== "waiting") {
        next.humanInput = null;
      }
      clearDraftForNonSuccessTerminal(next);
      break;
    case "progress":
      applyProgress(next, event.progress);
      break;
    case "model_attempt":
      applyModelAttempt(next, event);
      break;
    case "assistant_text":
      if (
        options.foregroundStreaming === true
        && !hasTerminalRunState(next)
        && isCurrentModelProducer(next, event.producer)
      ) {
        const delta = typeof event.delta === "string" ? event.delta : "";
        if (delta) {
          const previous = isObject(next.draft) && sameModelProducer(next.draft.producer, event.producer)
            ? stringOr(next.draft.text, "")
            : "";
          next.draft = {
            producer: clone(event.producer),
            text: previous + delta,
            updatedAt: stringOr(event.observed_at, isoNow()),
          };
          next.status = next.status === "waiting" ? next.status : "running";
        }
      }
      break;
    case "assistant_draft_replaced":
      applyDraftReplacement(next, event);
      break;
    case "tool":
      applyToolProgress(next, event.progress);
      break;
    case "sandbox":
      if (isObject(event.observation)) {
        next.sandboxObservations = mergeSandboxObservations(
          Array.isArray(next.sandboxObservations) ? next.sandboxObservations : [],
          [event.observation],
        );
      }
      break;
    case "human_input":
      applyHumanInputObservation(next, event);
      break;
    case "terminal": {
      const terminalStatus = uiStatus(event.status);
      next.terminalSignal = {
        errorCode: stringOr(event.error_code, ""),
        resultAvailable: event.result_available === true,
        status: terminalStatus,
        observedAt: stringOr(event.observed_at, isoNow()),
      };
      next.resultAvailable = event.result_available === true || next.resultAvailable === true;
      clearDraftForNonSuccessTerminal(next, terminalStatus);
      break;
    }
    case "degraded":
      if (isObject(event.health)) {
        next.observationHealth = normalizeObservationHealth(event.health);
      }
      break;
    default:
      throw new DurableChatProtocolError("The Durable Chat event type is unsupported.");
  }
  return next;
}

export class DurableChatApplication {
  constructor(shell, options = {}) {
    if (!shell) {
      throw new Error("A Durable Chat shell is required.");
    }
    this.shell = shell;
    this.location = options.location ?? globalThis.location;
    this.fetch = options.fetch ?? globalThis.fetch?.bind(globalThis);
    if (typeof this.fetch !== "function") {
      throw new Error("Fetch is required for Durable Chat.");
    }
    this.history = null;
    this.bootstrap = null;
    this.transport = null;
    this.authKey = "";
    this.selectedSessionId = "";
    this.selectedRequests = new Map();
    this.selectedSandboxProfile = "";
    this.sessions = new Map();
    this.requests = new Map();
    this.contexts = new Map();
    this.storageMessage = "";
    this._connected = false;
    this._starting = null;
    this._abortController = new AbortController();
    this._bound = false;
  }

  async start() {
    this._bindShellEvents();
    this.shell.renderSessions({ state: "loading" });
    this._renderActiveSession();
    await this.connect();
  }

  async connect() {
    if (this._starting) {
      return this._starting;
    }
    this._starting = this._connect().finally(() => {
      this._starting = null;
    });
    return this._starting;
  }

  destroy() {
    this._abortController.abort();
    for (const context of this.contexts.values()) {
      this._stopWatching(context);
    }
    this.contexts.clear();
    this.history?.close();
    this.history = null;
  }

  async _connect() {
    this.shell.setConnectionState({
      state: this._connected ? "reconnecting" : "loading",
      allowKeyEntry: false,
      message: "Connecting to the Function App.",
    });
    try {
      const configUrl = new URL("config", deriveDurableChatShellBase(this.location));
      this._assertDeploymentUrl(configUrl);
      const rawBootstrap = await this._requestConfig(configUrl);
      const bootstrap = normalizeDurableChatBootstrap(rawBootstrap, this.location);
      const namespaceChanged = this.bootstrap?.historyNamespace
        && this.bootstrap.historyNamespace !== bootstrap.historyNamespace;
      if (namespaceChanged) {
        this._clearLoadedState();
      }

      this.bootstrap = bootstrap;
      this.transport = new DurableChatTransport({
        bootstrap,
        deploymentRoot: deploymentRootFor(asHttpLocation(this.location)),
        fetch: this.fetch,
        getFunctionKey: () => this.authKey,
        location: this.location,
      });
      this.selectedSandboxProfile = bootstrap.supportedSandboxProfiles.includes(this.selectedSandboxProfile)
        ? this.selectedSandboxProfile
        : bootstrap.defaultSandboxProfile;
      this.shell.setSandboxProfiles({
        profiles: bootstrap.supportedSandboxProfiles.map(sandboxProfileOption),
        selectedProfile: this.selectedSandboxProfile,
        enabled: Boolean(this.selectedSessionId),
      });
      try {
        await this._openHistory(bootstrap.historyNamespace);
        await this._loadHistory();
      } catch (error) {
        if (!(error instanceof DurableChatHistoryError)) {
          throw error;
        }
        this._connected = false;
        this._showStorageNotice(
          `Saved browser history could not be opened (${error.code.replaceAll("_", " ")}). Existing history was not changed.`,
        );
        this.shell.setConnectionState({
          state: "ready",
          allowKeyEntry: true,
          configuredSandboxGroup: bootstrap.sandboxGroupResourceId,
          deploymentLabel: bootstrap.agent.displayName,
          message: `${bootstrap.agent.displayName} is reachable, but local history is unavailable.`,
        });
        this.shell.renderSessions({
          state: "error",
          error: "Browser history is unavailable. Resolve storage access before starting a request.",
        });
        this._renderActiveSession();
        return;
      }
      this._connected = true;
      this.shell.setConnectionState({
        state: "ready",
        allowKeyEntry: true,
        configuredSandboxGroup: bootstrap.sandboxGroupResourceId,
        deploymentLabel: bootstrap.agent.displayName,
        message: `${bootstrap.agent.displayName} is ready for durable chat requests.`,
      });
      this.shell.setConnectionPanelVisible(false);
      this._renderAll();
      await this._resumeStoredWork();
    } catch (error) {
      this._connected = false;
      this._renderConnectionFailure(error);
    }
  }

  async _requestConfig(configUrl) {
    const response = await this.fetch(configUrl.href, {
      cache: "no-store",
      credentials: "same-origin",
      headers: this._headers({ accept: "application/json" }),
      referrerPolicy: "no-referrer",
      redirect: "error",
      signal: this._abortController.signal,
    });
    const payload = await readJsonResponse(response);
    if (!response.ok) {
      throw new DurableChatHttpError(response.status, payload, "The Durable Chat bootstrap failed.");
    }
    return payload;
  }

  async _openHistory(namespace) {
    if (this.history?.namespace === namespace) {
      return;
    }
    this.history?.close();
    this.history = null;
    this.storageMessage = "";
    this.shell.setStorageNotice({ visible: false });
    this.history = await openDurableChatHistory({
      namespace,
      allowVolatile: true,
      announceVolatileMode: async () => {
        this._showStorageNotice(
          "Browser storage is unavailable. This page can continue, but its history will be lost when it closes.",
        );
      },
    });
    if (this.history.mode === "volatile") {
      this._showStorageNotice(
        "Browser storage is unavailable. This page can continue, but its history will be lost when it closes.",
      );
    }
  }

  async _loadHistory() {
    if (!this.history) {
      return;
    }
    for (const context of this.contexts.values()) {
      this._stopWatching(context);
    }
    this.sessions.clear();
    this.requests.clear();
    this.contexts.clear();
    const listed = await this.history.listSessions();
    for (const session of listed.sessions) {
      this.sessions.set(session.sessionId, session);
      const requestList = await this.history.listRequests(session.sessionId);
      const requestMap = new Map();
      this.requests.set(session.sessionId, requestMap);
      for (const request of requestList.requests) {
        requestMap.set(request.requestId, request);
        const context = this._createContext(request);
        this._registerContext(context);
      }
    }
    if (!this.sessions.has(this.selectedSessionId)) {
      this.selectedSessionId = listed.sessions[0]?.sessionId ?? "";
    }
  }

  async _resumeStoredWork() {
    const resumptions = [];
    for (const context of this.contexts.values()) {
      if (context.record.terminal) {
        continue;
      }
      if (!context.runId && isStartUncertain(context.projection)) {
        if (startAttempts(context.projection) < MAX_START_ATTEMPTS) {
          resumptions.push(this._submitPersistedStart(context, { reconciliation: true }));
        }
      } else if (context.runId) {
        this._watchContext(context);
      }
    }
    await Promise.allSettled(resumptions);
  }

  _bindShellEvents() {
    if (this._bound) {
      return;
    }
    this._bound = true;
    const shellElement = this.shell.elements.transcript.closest("#durable-chat-shell");
    const listen = (name, callback) => {
      shellElement.addEventListener(name, (event) => {
        void callback(event.detail ?? {});
      }, { signal: this._abortController.signal });
    };
    listen("durable-chat:new-session", () => this._createSession());
    listen("durable-chat:select-session", ({ sessionId }) => this._selectSession(sessionId));
    listen("durable-chat:rename-session", ({ sessionId }) => this._renameSession(sessionId));
    listen("durable-chat:commit-session-rename", ({ sessionId, title }) => this._renameSession(sessionId, title));
    listen("durable-chat:remove-session", ({ sessionId }) => this._removeSession(sessionId));
    listen("durable-chat:submit-message", ({ message }) => this._submitMessage(message));
    listen("durable-chat:cancel-request", ({ requestId }) => this._cancelRequest(requestId));
    listen("durable-chat:respond-human-input", (detail) => this._respondHumanInput(detail));
    listen("durable-chat:reconnect", () => this._reconnect());
    listen("durable-chat:submit-function-key", () => this._submitFunctionKey());
    listen("durable-chat:clear-function-key", () => this._clearFunctionKey());
    listen("durable-chat:request-selected", ({ requestId }) => this._selectRequest(requestId));
    listen("durable-chat:sandbox-profile-change", ({ sandboxProfile }) => {
      if (this.bootstrap?.supportedSandboxProfiles.includes(sandboxProfile)) {
        this.selectedSandboxProfile = sandboxProfile;
      }
    });
  }

  async _createSession() {
    if (!this._readyForHistory()) {
      this.shell.announce("Connect to the Function App before creating a session.");
      return;
    }
    const sessionId = createOpaqueIdentifier("session");
    try {
      const result = await this.history.createSession({
        sessionId,
        title: "New session",
      });
      if (!result.session) {
        throw new DurableChatHistoryError("storage_write_failed", "The session was not saved.");
      }
      this.sessions.set(sessionId, result.session);
      this.requests.set(sessionId, new Map());
      this.selectedSessionId = sessionId;
      this._renderAll();
      this.shell.elements.prompt.focus();
      this.shell.announce("New session created.");
    } catch (error) {
      this._handleHistoryFailure(error, "The session was not saved. No request was sent.");
    }
  }

  async _selectSession(sessionId) {
    if (!this.sessions.has(sessionId)) {
      return;
    }
    this.selectedSessionId = sessionId;
    this._renderAll();
  }

  async _renameSession(sessionId, submittedTitle) {
    const session = this.sessions.get(sessionId);
    if (!session || !this.history) {
      return;
    }
    if (submittedTitle === undefined) {
      this.shell.beginSessionRename(sessionId);
      return;
    }
    const title = typeof submittedTitle === "string" ? submittedTitle : "";
    if (!title.trim()) {
      this.shell.announce("Enter a session name to rename it.");
      return;
    }
    try {
      const result = await this.history.renameSession({
        sessionId,
        title,
        expectedVersion: session.version,
      });
      if (result.disposition === "version_conflict") {
        await this._refreshSession(sessionId);
        this.shell.announce("This session changed in another tab. Its latest name is shown.");
        return;
      }
      if (result.session) {
        this.sessions.set(sessionId, result.session);
        this._renderAll();
      }
    } catch (error) {
      this._handleHistoryFailure(error, "The session name was not saved.");
    }
  }

  async _removeSession(sessionId) {
    const session = this.sessions.get(sessionId);
    if (!session || !this.history) {
      return;
    }
    const confirmed = globalThis.confirm?.(
      `Remove the local history for “${session.title || "this session"}”? This does not cancel any server run.`,
    );
    if (confirmed !== true) {
      return;
    }
    try {
      const result = await this.history.removeSession({
        sessionId,
        confirmed: true,
        expectedVersion: session.version,
      });
      if (result.disposition === "version_conflict") {
        await this._refreshSession(sessionId);
        this.shell.announce("This session changed in another tab. It was not removed.");
        return;
      }
      if (result.disposition !== "removed") {
        return;
      }
      this.sessions.delete(sessionId);
      const removed = this.requests.get(sessionId);
      this.requests.delete(sessionId);
      this._forgetSelectedRequest(sessionId);
      if (removed) {
        for (const request of removed.values()) {
          const context = this._contextFor(sessionId, request.requestId);
          if (context) {
            context.persisted = false;
            this._stopWatching(context);
            this.contexts.delete(contextKey(sessionId, request.requestId));
          }
        }
      }
      if (this.selectedSessionId === sessionId) {
        this.selectedSessionId = Array.from(this.sessions.values())
          .sort(compareUpdatedSessions)[0]?.sessionId ?? "";
      }
      this._renderAll();
      this.shell.announce("Local session history removed. Any server run was not cancelled.");
    } catch (error) {
      this._handleHistoryFailure(error, "The local session history was not removed.");
    }
  }

  async _submitMessage(message) {
    if (!this._readyForHistory() || !this.selectedSessionId || typeof message !== "string" || !message.trim()) {
      this.shell.announce("Choose a session and enter a message before sending.");
      return;
    }
    if (utf8ByteLength(message) > MAX_PROMPT_UTF8_BYTES) {
      this.shell.announce("Messages must be 256 KiB or smaller when encoded as UTF-8.");
      return;
    }
    if (this._activeContextForSession(this.selectedSessionId)) {
      this.shell.announce("This session already has an active request. Start a new session to work in parallel.");
      return;
    }

    let body;
    try {
      body = this._createStartBody(this.selectedSessionId, message);
    } catch {
      this.shell.announce("This browser cannot create a safe request identifier.");
      return;
    }
    const requestId = body.request_id;
    const idempotencyKey = createOpaqueIdentifier("request-key");
    try {
      const result = await this.history.recordSubmission({
        sessionId: this.selectedSessionId,
        requestId,
        idempotencyKey,
        normalizedSubmission: body,
        transcript: [{
          role: "user",
          text: message,
          submittedAt: isoNow(),
        }],
        draft: null,
        projection: createInitialProjection({
          foregroundStreaming: this.bootstrap.foregroundStreamingAvailable,
          sessionId: this.selectedSessionId,
        }),
      });
      if (!result.request || !["recorded", "existing"].includes(result.disposition)) {
        this.shell.announce("This request conflicts with existing local history and was not sent.");
        return;
      }
      if (result.session) {
        this.sessions.set(this.selectedSessionId, result.session);
      }
      const context = this._createContext(result.request);
      this._registerContext(context);
      this._rememberSelectedRequest(this.selectedSessionId, requestId);
      this.shell.elements.prompt.value = "";
      this._renderAll();
      await this._submitPersistedStart(context);
    } catch (error) {
      this._handleHistoryFailure(
        error,
        "The request was not saved locally, so it was not sent to the Function App.",
      );
    }
  }

  _createStartBody(sessionId, prompt) {
    const requestId = createOpaqueIdentifier("request");
    return {
      schema_version: "1",
      fault_profile: "none",
      prompt,
      request_id: requestId,
      sandbox_profile: this.selectedSandboxProfile || this.bootstrap.defaultSandboxProfile,
      session_id: sessionId,
      ui: {
        schema_version: "1",
        stream_response: this.bootstrap.foregroundStreamingAvailable === true,
      },
    };
  }

  async _submitPersistedStart(context, options = {}) {
    if (!this.transport || context.record.terminal || context.runId) {
      return;
    }
    if (startAttempts(context.projection) >= MAX_START_ATTEMPTS) {
      await this._mutateContext(context, (projection) => {
        projection.startState = "uncertain";
        projection.status = "uncertain";
        projection.error = displayError(
          "start_confirmation_unresolved",
          "The request was saved, but its start could not be confirmed. It will not be sent again automatically.",
        );
      });
      return;
    }

    const persisted = await this._mutateContext(context, (projection) => {
      projection.startAttempts = startAttempts(projection) + 1;
      projection.startState = "pending";
      projection.status = "submitting";
      projection.error = null;
    });
    if (!persisted) {
      return;
    }

    try {
      const response = await this.transport.start(context.record.normalizedSubmission, context.record.idempotencyKey);
      await this._acceptStartResponse(context, response.payload);
    } catch (error) {
      await this._handleStartFailure(context, error, options);
    }
  }

  async _acceptStartResponse(context, payload) {
    if (!isObject(payload)) {
      throw new DurableChatProtocolError("The start response is invalid.");
    }
    const runId = stringOr(payload.run_id, "");
    if (!runId) {
      throw new DurableChatProtocolError("The start response does not include a run ID.");
    }
    const returnedSessionId = stringOr(payload.session_id, context.sessionId);
    if (returnedSessionId !== context.sessionId) {
      throw new DurableChatProtocolError("The start response is bound to a different session.");
    }

    if (typeof payload.response === "string" && payload.status === "Completed") {
      context.runId = runId;
      await this._mutateContext(context, (projection) => {
        projection.runId = runId;
        projection.startState = "accepted";
        projection.status = "completed";
        projection.finalResponse = payload.response;
        projection.draft = null;
        projection.error = null;
        projection.completionPending = false;
        projection.terminalConfirmed = true;
      }, { terminal: true });
      return;
    }

    context.runId = runId;
    const uncertain = payload.possibly_committed === true || payload.error === "run_start_acknowledgement_lost";
    await this._mutateContext(context, (projection) => {
      projection.runId = runId;
      projection.startState = uncertain ? "uncertain" : "accepted";
      projection.status = uncertain ? "uncertain" : uiStatus(payload.status);
      projection.error = uncertain
        ? displayError("run_start_acknowledgement_lost", "The run may have started. Checking its durable status.")
        : null;
      projection.foregroundStreaming = this.bootstrap.foregroundStreamingAvailable === true;
    });
    this._watchContext(context);
    void this._loadDiagnostics(context);
  }

  async _handleStartFailure(context, error, options) {
    const http = error instanceof DurableChatHttpError ? error : null;
    const code = errorCode(http?.payload);
    if (code === "session_busy") {
      await this._mutateContext(context, (projection) => {
        projection.startState = "rejected";
        projection.status = "session_busy";
        projection.error = displayError(
          code,
          "This session already has an active server request. Start a new session to work in parallel.",
        );
      }, { terminal: true });
      return;
    }
    if (http && (http.status === 401 || http.status === 403)) {
      await this._mutateContext(context, (projection) => {
        projection.startState = "uncertain";
        projection.status = "uncertain";
        projection.error = displayError("authentication_required", "Authentication is required to confirm this request.");
      });
      this._renderConnectionFailure(http);
      return;
    }
    if (http && http.status === 410) {
      await this._mutateContext(context, (projection) => {
        projection.startState = "rejected";
        projection.status = "expired";
        projection.error = displayError(code || "run_unavailable", "The request is no longer available.");
      }, { terminal: true });
      return;
    }
    if (http && code === "idempotency_conflict") {
      await this._mutateContext(context, (projection) => {
        projection.startState = "rejected";
        projection.status = "failed";
        projection.error = displayError(code, "The saved request conflicts with a server request and was not started.");
      }, { terminal: true });
      return;
    }

    await this._mutateContext(context, (projection) => {
      projection.startState = "uncertain";
      projection.status = "uncertain";
      projection.error = displayError(
        code || "start_unconfirmed",
        "The request was saved, but its start was not confirmed. Retrying the same saved request once.",
      );
    });
    if (!options.reconciliation && startAttempts(context.projection) < MAX_START_ATTEMPTS) {
      context.startRetryTimer = globalThis.setTimeout(() => {
        void this._submitPersistedStart(context, { reconciliation: true });
      }, EVENT_RECONNECT_MILLISECONDS);
    }
  }

  _watchContext(context) {
    if (!context.runId || context.record.terminal || context.stopped) {
      return;
    }
    if (!context.statusTimer) {
      this._scheduleStatusPoll(context, 0);
    }
    if (!context.streamTask) {
      context.streamTask = this._consumeRunEvents(context)
        .catch(() => undefined)
        .finally(() => {
          context.streamTask = null;
        });
    }
  }

  _scheduleStatusPoll(context, delay) {
    if (context.stopped || context.record.terminal) {
      return;
    }
    context.statusTimer = globalThis.setTimeout(async () => {
      context.statusTimer = null;
      await this._refreshRunStatus(context);
      if (!context.stopped && !context.record.terminal) {
        this._scheduleStatusPoll(context, STATUS_POLL_MILLISECONDS);
      }
    }, delay);
  }

  async _consumeRunEvents(context) {
    let failures = 0;
    while (!context.stopped && !context.record.terminal && context.runId) {
      try {
        context.streamAbortController = new AbortController();
        const response = await this.transport.events(
          context.runId,
          context.streamCursor,
          context.streamAbortController.signal,
        );
        failures = 0;
        await consumeSseResponse(response, async (frame) => {
          await this._applySseFrame(context, frame);
        });
        context.streamAbortController = null;
        await this._refreshRunStatus(context);
        if (!context.stopped && !context.record.terminal) {
          await delay(EVENT_RECONNECT_MILLISECONDS);
        }
      } catch (error) {
        context.streamAbortController = null;
        if (isAbortError(error) || context.stopped) {
          return;
        }
        if (error instanceof DurableChatHttpError && error.payload.error === "event_cursor_ahead") {
          await this._recoverFromFutureCursor(context, error.payload);
          continue;
        }
        if (error instanceof DurableChatHttpError && [401, 403].includes(error.status)) {
          this._renderConnectionFailure(error);
          return;
        }
        failures += 1;
        await this._mutateContext(context, (projection) => {
          projection.streamState = failures >= MAX_STREAM_FAILURES ? "status_only" : "reconnecting";
          if (error instanceof DurableChatHttpError && error.status === 410) {
            projection.observationHealth = {
              degraded: true,
              reasons: ["chat_observations_expired"],
            };
            projection.error = displayError(
              "chat_observations_expired",
              "Live chat observations have expired. Checking the durable run status instead.",
            );
          }
        });
        if (failures >= MAX_STREAM_FAILURES) {
          return;
        }
        await delay(EVENT_RECONNECT_MILLISECONDS * failures);
      }
    }
  }

  async _applySseFrame(context, frame) {
    const payload = parseSsePayload(frame);
    if (payload.kind === "snapshot") {
      const snapshot = payload.snapshot;
      if (snapshot.projection.run_id !== context.runId || snapshot.projection.session_id !== context.sessionId) {
        throw new DurableChatProtocolError("The snapshot is bound to a different request.");
      }
      const cursor = cursorFromSnapshot(snapshot);
      if (!context.forceCursorReset && cursor.position < context.streamCursor) {
        return;
      }
      const reset = context.forceCursorReset;
      const applied = await this._queueContext(context, () => this._persistProjection(
        context,
        projectionFromSnapshot(
          context.projection,
          snapshot.projection,
          this.bootstrap.foregroundStreamingAvailable,
        ),
        {
          authoritativeReset: reset,
          cursor,
        },
      ));
      if (applied) {
        context.forceCursorReset = false;
        context.replayBase = null;
        context.streamCursor = cursor.position;
      }
      return;
    }

    const eventFrame = payload.frame;
    if (eventFrame.event.run_id !== context.runId || eventFrame.event.session_id !== context.sessionId) {
      throw new DurableChatProtocolError("The event is bound to a different request.");
    }
    if (!context.forceCursorReset && eventFrame.sequence <= context.streamCursor) {
      return;
    }
    const reset = context.forceCursorReset;
    const publishedRevision = optionalEventPublishedRevision(eventFrame);
    const cursor = cursorFromEvent(eventFrame, publishedRevision);
    const applied = await this._queueContext(context, () => {
      const base = reset ? context.replayBase : context.projection;
      const next = reduceDurableChatProjection(base, eventFrame.event, {
        foregroundStreaming: this.bootstrap.foregroundStreamingAvailable,
      });
      if (publishedRevision !== null) {
        next.serverPublishedRevision = publishedRevision;
      }
      return this._persistProjection(context, next, {
        authoritativeReset: reset,
        cursor,
        mode: reset ? "snapshot" : "incremental",
      });
    });
    if (!applied) {
      return;
    }
    context.forceCursorReset = false;
    context.replayBase = null;
    context.streamCursor = eventFrame.sequence;
    if (eventFrame.event.event_type === "human_input") {
      void this._loadHumanInputDetail(context);
    }
    if (eventFrame.event.event_type === "terminal") {
      await this._refreshRunStatus(context);
    }
  }

  async _recoverFromFutureCursor(context, payload) {
    const throughSequence = nonnegativeInteger(payload.through_sequence);
    if (throughSequence === null) {
      throw new DurableChatProtocolError("The event cursor recovery response is invalid.");
    }
    context.forceCursorReset = true;
    context.replayBase = createInitialProjection({
      foregroundStreaming: this.bootstrap.foregroundStreamingAvailable,
      runId: context.runId,
      sessionId: context.sessionId,
      status: uiStatus(context.projection.status),
    });
    context.streamCursor = 0;
    await this._mutateContext(context, (projection) => {
      projection.streamState = "reconnecting";
      projection.error = displayError(
        "event_cursor_ahead",
        "Local live-observation history is ahead of the server. Rebuilding this request from an authoritative replay.",
      );
      projection.serverThroughSequence = throughSequence;
    });
  }

  async _refreshRunStatus(context) {
    if (!context.runId || context.stopped || context.record.terminal || context.statusInFlight) {
      return;
    }
    context.statusInFlight = true;
    try {
      const response = await this.transport.status(context.runId);
      const status = response.payload;
      if (!isObject(status) || status.run_id !== context.runId || status.session_id !== context.sessionId) {
        throw new DurableChatProtocolError("The status response is bound to a different request.");
      }
      const serverStatus = uiStatus(status.status);
      const applied = await this._mutateContext(context, (projection) => {
        applyAuthoritativeStatus(projection, status);
        projection.streamState = projection.streamState === "status_only"
          ? "status_only"
          : "connected";
      });
      if (applied) {
        if (serverStatus === "waiting") {
          void this._loadHumanInputDetail(context);
        }
        if (serverStatus === "completed") {
          await this._loadFinalResult(context);
        } else if (serverStatus === "failed" || serverStatus === "cancelled") {
          await this._mutateContext(context, (projection) => {
            projection.terminalConfirmed = true;
            projection.completionPending = false;
          }, { terminal: true });
          this._queueFinalDiagnosticsRefresh(context);
          this._stopWatching(context);
        }
      }
    } catch (error) {
      if (isAbortError(error) || context.stopped) {
        return;
      }
      if (error instanceof DurableChatHttpError && error.status === 404 && isStartUncertain(context.projection)) {
        if (startAttempts(context.projection) < MAX_START_ATTEMPTS) {
          await this._submitPersistedStart(context, { reconciliation: true });
        }
        return;
      }
      if (error instanceof DurableChatHttpError && [401, 403].includes(error.status)) {
        this._renderConnectionFailure(error);
        return;
      }
      if (error instanceof DurableChatHttpError && error.status === 410) {
        await this._mutateContext(context, (projection) => {
          projection.status = "expired";
          projection.error = displayError("run_expired", "This durable run is no longer available.");
          projection.terminalConfirmed = true;
        }, { terminal: true });
        this._queueFinalDiagnosticsRefresh(context);
        this._stopWatching(context);
        return;
      }
      await this._mutateContext(context, (projection) => {
        projection.streamState = "reconnecting";
        projection.error = displayError(
          errorCode(error?.payload) || "status_unavailable",
          "The durable run status could not be refreshed. Retrying status checks.",
        );
      });
    } finally {
      context.statusInFlight = false;
    }
  }

  async _loadFinalResult(context) {
    if (!context.runId || context.resultInFlight || context.record.terminal) {
      return;
    }
    context.resultInFlight = true;
    try {
      const response = await this.transport.result(context.runId);
      if (response.status === 202) {
        await this._mutateContext(context, (projection) => {
          projection.status = "completed";
          projection.completionPending = true;
          projection.error = null;
        });
        return;
      }
      const result = response.payload;
      if (
        !isObject(result)
        || result.run_id !== context.runId
        || result.session_id !== context.sessionId
        || result.status !== "Completed"
        || typeof result.response !== "string"
      ) {
        throw new DurableChatProtocolError("The final result is invalid.");
      }
      await this._mutateContext(context, (projection) => {
        projection.status = "completed";
        projection.finalResponse = result.response;
        projection.draft = null;
        projection.error = null;
        projection.completionPending = false;
        projection.terminalConfirmed = true;
        projection.humanInput = null;
      }, { terminal: true });
      this._queueFinalDiagnosticsRefresh(context);
      this._stopWatching(context);
    } catch (error) {
      if (isAbortError(error) || context.stopped) {
        return;
      }
      if (error instanceof DurableChatHttpError && (error.status === 409 || error.status === 410)) {
        await this._mutateContext(context, (projection) => {
          projection.status = error.status === 410 ? "expired" : "failed";
          projection.error = displayError(
            errorCode(error.payload) || "result_unavailable",
            error.status === 410
              ? "The completed run result is no longer available."
              : "The durable run did not produce a completed result.",
          );
          projection.terminalConfirmed = true;
        }, { terminal: true });
        this._queueFinalDiagnosticsRefresh(context);
        this._stopWatching(context);
      } else if (error instanceof DurableChatHttpError && [401, 403].includes(error.status)) {
        await this._mutateContext(context, (projection) => {
          projection.completionPending = true;
          projection.error = displayError(
            "authentication_required",
            "Authentication is required to retrieve the completed response. Reconnect and provide a Function key if this app uses key authentication.",
          );
        });
        this._renderConnectionFailure(error);
      } else {
        const isProtocolFailure = error instanceof DurableChatProtocolError;
        await this._mutateContext(context, (projection) => {
          projection.completionPending = true;
          projection.error = displayError(
            isProtocolFailure ? "result_protocol_invalid" : errorCode(error?.payload) || "result_unavailable",
            isProtocolFailure
              ? "The completed response did not match the Durable Chat protocol. Retrying result retrieval."
              : "The completed response could not be retrieved. Retrying result retrieval.",
          );
        });
      }
    } finally {
      context.resultInFlight = false;
    }
  }

  async _loadHumanInputDetail(context) {
    const human = context.projection.humanInput;
    if (
      !context.runId
      || !isObject(human)
      || human.state !== "pending"
      || !nonEmptyString(human.requestId)
      || context.humanInputInFlight === human.requestId
    ) {
      return;
    }
    if (context.humanDetails?.requestId === human.requestId) {
      return;
    }
    context.humanInputInFlight = human.requestId;
    try {
      const response = await this.transport.humanInputDetail(context.runId, human.requestId);
      const detail = response.payload;
      if (
        !isObject(detail)
        || detail.run_id !== context.runId
        || detail.request_id !== human.requestId
        || typeof detail.question !== "string"
        || !Array.isArray(detail.choices)
        || typeof detail.allow_free_text !== "boolean"
      ) {
        throw new DurableChatProtocolError("The human-input detail is invalid.");
      }
      context.humanDetails = {
        allowFreeText: detail.allow_free_text,
        choices: detail.choices.filter((choice) => typeof choice === "string"),
        expiresAt: stringOr(detail.expires_at, ""),
        question: detail.question,
        requestId: detail.request_id,
        responseSchema: isObject(detail.response_schema) ? clone(detail.response_schema) : null,
      };
      await this._mutateContext(context, (projection) => {
        if (projection.humanInput?.requestId === detail.request_id) {
          projection.humanInput.question = detail.question;
          projection.humanInput.allowFreeText = detail.allow_free_text;
          projection.humanInput.choices = detail.choices.filter((choice) => typeof choice === "string");
          projection.humanInput.schemaPresent = isObject(detail.response_schema);
          projection.humanInput.expiresAt = stringOr(detail.expires_at, projection.humanInput.expiresAt);
        }
      });
    } catch (error) {
      if (error instanceof DurableChatHttpError && error.status === 410) {
        await this._mutateContext(context, (projection) => {
          if (projection.humanInput?.requestId === human.requestId) {
            projection.humanInput.state = "gone";
          }
        });
      } else {
        await this._mutateContext(context, (projection) => {
          projection.error = displayError(
            errorCode(error?.payload) || "human_input_unavailable",
            "The requested input could not be loaded. Refreshing the durable run status.",
          );
        });
      }
    } finally {
      context.humanInputInFlight = "";
    }
  }

  async _respondHumanInput(detail) {
    const context = this._contextFor(this.selectedSessionId, detail.requestId);
    if (!context || !context.runId || !isObject(context.projection.humanInput)) {
      return;
    }
    const human = context.projection.humanInput;
    if (human.state !== "pending" || human.requestId !== detail.inputId) {
      return;
    }
    let answer;
    try {
      answer = normalizeHumanAnswer(context, detail);
    } catch (error) {
      this.shell.announce(error.message);
      return;
    }
    const submissionKey = createOpaqueIdentifier("human-input");
    const marked = await this._mutateContext(context, (projection) => {
      if (projection.humanInput?.requestId === human.requestId) {
        projection.humanInput.submitting = true;
        projection.error = null;
      }
    });
    if (!marked) {
      return;
    }
    try {
      const response = await this.transport.submitHumanInput(
        context.runId,
        human.requestId,
        answer,
        submissionKey,
      );
      if (!isObject(response.payload) || response.payload.request_id !== human.requestId) {
        throw new DurableChatProtocolError("The human-input response is invalid.");
      }
      await this._mutateContext(context, (projection) => {
        if (projection.humanInput?.requestId === human.requestId) {
          projection.humanInput.state = "answered";
          projection.humanInput.submitting = false;
        }
      });
      await this._refreshRunStatus(context);
    } catch (error) {
      await this._mutateContext(context, (projection) => {
        if (projection.humanInput?.requestId === human.requestId) {
          projection.humanInput.submitting = false;
        }
        projection.error = displayError(
          errorCode(error?.payload) || "human_input_not_accepted",
          "The input response was not accepted. Check the request and try again.",
        );
      });
    }
  }

  async _cancelRequest(requestId) {
    const context = this._contextFor(this.selectedSessionId, requestId);
    if (!context?.runId || context.record.terminal) {
      return;
    }
    const marked = await this._mutateContext(context, (projection) => {
      projection.cancelRequested = true;
      projection.status = "cancel_requested";
      projection.error = null;
    });
    if (!marked) {
      return;
    }
    try {
      await this.transport.cancel(context.runId);
      await this._refreshRunStatus(context);
    } catch (error) {
      if (error instanceof DurableChatHttpError && error.status === 410) {
        await this._refreshRunStatus(context);
        return;
      }
      await this._mutateContext(context, (projection) => {
        projection.cancelRequested = false;
        projection.error = displayError(
          errorCode(error?.payload) || "cancel_unavailable",
          "The cancellation request was not accepted. The durable run may still be active.",
        );
      });
    }
  }

  async _selectRequest(requestId) {
    if (!this.selectedSessionId || !this._requestMap(this.selectedSessionId).has(requestId)) {
      return;
    }
    this._rememberSelectedRequest(this.selectedSessionId, requestId);
    this._renderActiveSession();
    const context = this._contextFor(this.selectedSessionId, requestId);
    if (context) {
      void this._loadDiagnostics(context);
      if (context.projection.humanInput?.state === "pending") {
        void this._loadHumanInputDetail(context);
      }
    }
  }

  _queueFinalDiagnosticsRefresh(context) {
    if (
      !this._isCurrentContext(context)
      || !context.runId
      || !this.transport
      || context.finalDiagnosticsRefreshRequested
    ) {
      return;
    }
    context.finalDiagnosticsRefreshRequested = true;
    void this._loadDiagnostics(context);
  }

  async _loadDiagnostics(context) {
    if (
      !this._isCurrentContext(context)
      || !context.runId
      || context.diagnosticsInFlight
      || !this.transport
    ) {
      return;
    }
    const finalRefresh = (
      context.finalDiagnosticsRefreshRequested
      && !context.finalDiagnosticsRefreshStarted
    );
    if (finalRefresh) {
      context.finalDiagnosticsRefreshStarted = true;
    }
    context.diagnosticsInFlight = true;
    try {
      const response = await this.transport.diagnostics(context.runId);
      if (!this._isCurrentContext(context)) {
        return;
      }
      const diagnostics = response.payload;
      if (
        !isObject(diagnostics)
        || diagnostics.run_id !== context.runId
        || diagnostics.session_id !== context.sessionId
      ) {
        throw new DurableChatProtocolError("The diagnostics response is bound to a different request.");
      }
      const storedDiagnostics = normalizeDiagnostics(diagnostics);
      await this._mutateContext(context, (projection) => {
        projection.diagnostics = storedDiagnostics;
        projection.sandboxObservations = mergeSandboxObservations(
          Array.isArray(projection.sandboxObservations) ? projection.sandboxObservations : [],
          storedDiagnostics.sandboxObservations,
        );
        projection.observationHealth = storedDiagnostics.observationHealth;
      }, { terminal: finalRefresh });
    } catch (error) {
      if (!this._isCurrentContext(context)) {
        return;
      }
      if (error instanceof DurableChatHttpError && error.status === 404) {
        context.diagnosticsAttempts += 1;
        if (!context.record.terminal && context.diagnosticsAttempts < 4) {
          context.diagnosticsRetryTimer = globalThis.setTimeout(() => {
            context.diagnosticsRetryTimer = null;
            void this._loadDiagnostics(context);
          }, STATUS_POLL_MILLISECONDS);
        }
        return;
      }
      if (error instanceof DurableChatHttpError && error.status === 410) {
        await this._mutateContext(context, (projection) => {
          projection.diagnostics = preserveExpiredDiagnostics(projection.diagnostics);
        }, { terminal: finalRefresh });
      }
    } finally {
      context.diagnosticsInFlight = false;
      if (
        context.finalDiagnosticsRefreshRequested
        && !context.finalDiagnosticsRefreshStarted
        && this._isCurrentContext(context)
      ) {
        void this._loadDiagnostics(context);
      }
    }
  }

  async _reconnect() {
    await this.connect();
    if (!this._connected) {
      return;
    }
    for (const context of this.contexts.values()) {
      if (!context.record.terminal && context.runId && !context.stopped) {
        this._watchContext(context);
      }
      if (!context.record.terminal && !context.runId && isStartUncertain(context.projection)) {
        await this._submitPersistedStart(context, { reconciliation: true });
      }
    }
  }

  async _submitFunctionKey() {
    const key = this.shell.getFunctionKey();
    if (!key.trim()) {
      this.shell.announce("Enter a Function key before connecting.");
      return;
    }
    this._clearLoadedState();
    this.authKey = key;
    this.shell.clearFunctionKey();
    await this.connect();
  }

  async _clearFunctionKey() {
    this.authKey = "";
    this._clearLoadedState();
    await this.connect();
  }

  _clearLoadedState() {
    for (const context of this.contexts.values()) {
      this._stopWatching(context);
    }
    this.contexts.clear();
    this.sessions.clear();
    this.requests.clear();
    this.selectedSessionId = "";
    this.selectedRequests.clear();
    this.history?.close();
    this.history = null;
  }

  _renderConnectionFailure(error) {
    const authenticationRequired = error instanceof DurableChatHttpError
      && (error.status === 401 || error.status === 403);
    const message = authenticationRequired
      ? "Authentication is required. Enter a Function key if this app uses key authentication."
      : "The Function App connection could not be established. Use reconnect to try again.";
    this.shell.setConnectionState({
      state: "error",
      allowKeyEntry: true,
      message,
      announce: true,
    });
    this.shell.setConnectionPanelVisible(true);
    if (!this.history) {
      this.shell.renderSessions({
        state: "error",
        error: authenticationRequired
          ? "Authenticate to load local session history for this deployment."
          : "Reconnect to load saved sessions.",
      });
      this._renderActiveSession();
    }
  }

  _showStorageNotice(message) {
    this.storageMessage = message;
    this.shell.setStorageNotice({ visible: true, message });
    this.shell.announce(message);
  }

  _handleHistoryFailure(error, message) {
    if (error instanceof DurableChatHistoryError) {
      this._showStorageNotice(`${message} Browser storage reported ${error.code.replaceAll("_", " ")}.`);
    } else {
      this._showStorageNotice(message);
    }
  }

  _readyForHistory() {
    return Boolean(this._connected && this.bootstrap && this.history);
  }

  _createContext(record) {
    const body = isObject(record.normalizedSubmission) ? clone(record.normalizedSubmission) : {};
    const projection = projectionFromStored(
      record.projection,
      record.sessionId,
      this.bootstrap?.foregroundStreamingAvailable === true,
    );
    const runId = stringOr(projection.runId, "");
    return {
      cursor: isObject(record.cursor) ? clone(record.cursor) : null,
      diagnosticsAttempts: 0,
      diagnosticsInFlight: false,
      diagnosticsRetryTimer: null,
      finalDiagnosticsRefreshRequested: false,
      finalDiagnosticsRefreshStarted: false,
      forceCursorReset: false,
      humanDetails: null,
      humanInputInFlight: "",
      mutations: Promise.resolve(),
      persisted: true,
      projection,
      record: clone(record),
      replayBase: null,
      resultInFlight: false,
      runId,
      sessionId: record.sessionId,
      startRetryTimer: null,
      statusInFlight: false,
      statusTimer: null,
      stopped: false,
      streamAbortController: null,
      streamCursor: isObject(record.cursor) && Number.isInteger(record.cursor.position)
        ? record.cursor.position
        : 0,
      streamTask: null,
    };
  }

  _registerContext(context) {
    this._requestMap(context.sessionId).set(context.record.requestId, context.record);
    this.contexts.set(contextKey(context.sessionId, context.record.requestId), context);
  }

  _contextFor(sessionId, requestId) {
    return this.contexts.get(contextKey(sessionId, requestId));
  }

  _rememberSelectedRequest(sessionId, requestId) {
    this.selectedRequests.set(sessionId, requestId);
    if (this.history?.mode !== "persistent" || !this.bootstrap?.historyNamespace) {
      return;
    }
    try {
      globalThis.localStorage?.setItem(
        selectedRequestStorageKey(this.bootstrap.historyNamespace, sessionId),
        requestId,
      );
    } catch {
      return;
    }
  }

  _selectedRequestFor(sessionId) {
    const selected = this.selectedRequests.get(sessionId);
    if (selected) {
      return selected;
    }
    if (this.history?.mode !== "persistent" || !this.bootstrap?.historyNamespace) {
      return "";
    }
    try {
      return stringOr(
        globalThis.localStorage?.getItem(
          selectedRequestStorageKey(this.bootstrap.historyNamespace, sessionId),
        ),
        "",
      );
    } catch {
      return "";
    }
  }

  _forgetSelectedRequest(sessionId) {
    this.selectedRequests.delete(sessionId);
    if (this.history?.mode !== "persistent" || !this.bootstrap?.historyNamespace) {
      return;
    }
    try {
      globalThis.localStorage?.removeItem(
        selectedRequestStorageKey(this.bootstrap.historyNamespace, sessionId),
      );
    } catch {
      return;
    }
  }

  _isCurrentContext(context) {
    if (!context) {
      return false;
    }
    const requests = this.requests.get(context.sessionId);
    return (
      this.contexts.get(contextKey(context.sessionId, context.record.requestId)) === context
      && requests?.get(context.record.requestId) === context.record
    );
  }

  _requestMap(sessionId) {
    if (!this.requests.has(sessionId)) {
      this.requests.set(sessionId, new Map());
    }
    return this.requests.get(sessionId);
  }

  _activeContextForSession(sessionId) {
    for (const context of this._requestMap(sessionId).values()) {
      const live = this._contextFor(sessionId, context.requestId);
      if (live && !live.record.terminal && ACTIVE_UI_STATUSES.has(viewStatus(live.projection))) {
        return live;
      }
    }
    return null;
  }

  async _mutateContext(context, mutate, options = {}) {
    return this._queueContext(context, async () => {
      if (!this._isCurrentContext(context)) {
        return false;
      }
      const previousError = isObject(context.projection.error)
        ? context.projection.error
        : null;
      const next = clone(context.projection);
      mutate(next);
      next.updatedAt = isoNow();
      const persisted = await this._persistProjection(context, next, options);
      const currentError = isObject(context.projection.error)
        ? context.projection.error
        : null;
      if (
        context.sessionId === this.selectedSessionId
        && currentError
        && (
          currentError.code !== previousError?.code
          || currentError.message !== previousError?.message
        )
      ) {
        this.shell.announce(currentError.message);
      }
      return persisted;
    });
  }

  _queueContext(context, operation) {
    const queued = context.mutations.then(operation, operation);
    context.mutations = queued.catch(() => undefined);
    return queued;
  }

  async _persistProjection(context, next, options) {
    if (!this._isCurrentContext(context)) {
      return false;
    }
    if (next.terminalConfirmed === true) {
      next.terminalSignal = null;
    }
    context.projection = clone(next);
    const cursor = clone(options.cursor ?? context.cursor ?? zeroCursor(context.runId));
    const terminal = options.terminal === true;
    if (!this.history || !context.persisted) {
      this._renderIfVisible(context.sessionId);
      return true;
    }

    try {
      const result = await this.history.applyProjection({
        sessionId: context.sessionId,
        requestId: context.record.requestId,
        cursor,
        projection: context.projection,
        transcript: transcriptFor(context.record, context.projection),
        draft: context.projection.draft ?? null,
        terminal,
        expectedVersion: context.record.version,
        mode: options.mode ?? "incremental",
        authoritativeReset: options.authoritativeReset === true,
      });
      if (!this._isCurrentContext(context)) {
        return false;
      }
      if (result.request) {
        context.record = result.request;
        context.cursor = clone(result.request.cursor);
        this._requestMap(context.sessionId).set(context.record.requestId, context.record);
      }
      if (result.session) {
        this.sessions.set(context.sessionId, result.session);
      }
      if (["applied", "already_applied"].includes(result.disposition)) {
        this._renderIfVisible(context.sessionId);
        return true;
      }
      if (["version_conflict", "cursor_conflict", "stale_cursor", "terminal_preserved"].includes(result.disposition)) {
        await this._refreshContext(context);
      }
      this._renderIfVisible(context.sessionId);
      return false;
    } catch (error) {
      if (!this._isCurrentContext(context)) {
        return false;
      }
      context.persisted = false;
      this._handleHistoryFailure(
        error,
        "Live request updates can continue, but browser history is no longer being saved.",
      );
      this._renderIfVisible(context.sessionId);
      return options.requirePersistence !== true;
    }
  }

  async _refreshContext(context) {
    if (!this._isCurrentContext(context) || !this.history || !context.persisted) {
      return;
    }
    try {
      const result = await this.history.getRequest(context.sessionId, context.record.requestId);
      if (!this._isCurrentContext(context)) {
        return;
      }
      if (!result.request) {
        context.persisted = false;
        this._showStorageNotice("This request was removed locally in another tab. Live updates will not be retained.");
        return;
      }
      context.record = result.request;
      context.projection = projectionFromStored(
        result.request.projection,
        context.sessionId,
        this.bootstrap?.foregroundStreamingAvailable === true,
      );
      context.cursor = isObject(result.request.cursor) ? clone(result.request.cursor) : null;
      context.streamCursor = context.cursor?.position ?? 0;
      context.runId = stringOr(context.projection.runId, context.runId);
      this._requestMap(context.sessionId).set(context.record.requestId, result.request);
    } catch (error) {
      this._handleHistoryFailure(error, "Local request history could not be refreshed.");
    }
  }

  async _refreshSession(sessionId) {
    if (!this.history) {
      return;
    }
    const result = await this.history.getSession(sessionId);
    if (result.session) {
      this.sessions.set(sessionId, result.session);
      this._renderAll();
    }
  }

  _renderAll() {
    const sessions = Array.from(this.sessions.values()).sort(compareUpdatedSessions);
    this.shell.renderSessions({
      sessions: sessions.map((session) => ({
        id: session.sessionId,
        status: this._latestSessionStatus(session.sessionId),
        title: session.title,
      })),
      historyMode: this.history?.mode,
      selectedSessionId: this.selectedSessionId,
      state: this.history ? "ready" : "loading",
    });
    this._renderActiveSession();
  }

  _renderIfVisible(_sessionId) {
    this._renderAll();
  }

  _renderActiveSession() {
    const session = this.sessions.get(this.selectedSessionId);
    const requests = session
      ? Array.from(this._requestMap(session.sessionId).values())
        .sort(compareRequests)
        .map((record) => this._requestView(record))
      : [];
    let selectedRequestId = this._selectedRequestFor(this.selectedSessionId);
    if (!requests.some((request) => request.id === selectedRequestId)) {
      selectedRequestId = requests.at(-1)?.id ?? "";
      if (selectedRequestId) {
        this._rememberSelectedRequest(this.selectedSessionId, selectedRequestId);
      } else {
        this._forgetSelectedRequest(this.selectedSessionId);
      }
    } else {
      this.selectedRequests.set(this.selectedSessionId, selectedRequestId);
    }
    const active = session ? this._activeContextForSession(session.sessionId) : null;
    const ready = Boolean(session && this._readyForHistory() && !active);
    this.shell.renderSession({
      id: session?.sessionId ?? "",
      title: session?.title ?? "",
      status: session ? this._latestSessionStatus(session.sessionId) : "",
      composer: {
        cancelable: Boolean(active?.runId),
        enabled: ready,
        message: active
          ? "This session has an active request. Start a new session to work in parallel."
          : ready
            ? "Durable execution sends one request at a time."
            : "Choose or create a session to compose a request.",
        placeholder: active
          ? "Wait for this request or start a new session."
          : ready
            ? "Ask your agent to do something…"
            : "Choose a session to begin.",
        requestId: active?.record.requestId ?? "",
      },
    });
    this.shell.setSandboxProfiles({
      profiles: this.bootstrap?.supportedSandboxProfiles.map(sandboxProfileOption) ?? [],
      selectedProfile: this.selectedSandboxProfile,
      enabled: ready,
    });
    this.shell.renderRequests({
      requests,
      selectedRequestId,
      state: session ? "ready" : "empty",
      revealSelected: false,
    });
  }

  _latestSessionStatus(sessionId) {
    const records = Array.from(this._requestMap(sessionId).values()).sort(compareRequests);
    const latest = records.at(-1);
    if (!latest) {
      return "";
    }
    return viewStatus(this._contextFor(sessionId, latest.requestId)?.projection ?? latest.projection);
  }

  _requestView(record) {
    const context = this._contextFor(record.sessionId, record.requestId);
    const projection = context?.projection ?? projectionFromStored(
      record.projection,
      record.sessionId,
      this.bootstrap?.foregroundStreamingAvailable === true,
    );
    const status = viewStatus(projection);
    const finalResponse = stringOr(projection.finalResponse, "");
    const draft = isObject(projection.draft) ? stringOr(projection.draft.text, "") : "";
    return {
      canCancel: Boolean(context?.runId && !record.terminal && ACTIVE_UI_STATUSES.has(status)),
      completionPending: projection.completionPending === true,
      diagnostics: diagnosticsView(projection.diagnostics),
      error: isObject(projection.error) ? { message: projection.error.message } : undefined,
      humanInput: humanInputView(projection.humanInput, context?.humanDetails),
      id: record.requestId,
      progress: progressView(projection),
      prompt: stringOr(record.normalizedSubmission?.prompt, ""),
      response: finalResponse
        ? { text: finalResponse, mode: projection.foregroundStreaming ? "foreground" : "background" }
        : projection.completionPending
          ? { mode: "background" }
          : draft
            ? { text: draft, mode: "foreground", draft: true }
            : undefined,
      runId: stringOr(projection.runId, ""),
      sandbox: sandboxView(
        projection.sandboxObservations,
        projection.diagnostics?.configuredSandboxGroupResourceId,
      ),
      status,
    };
  }

  _stopWatching(context) {
    context.stopped = true;
    if (context.startRetryTimer) {
      globalThis.clearTimeout(context.startRetryTimer);
      context.startRetryTimer = null;
    }
    if (context.statusTimer) {
      globalThis.clearTimeout(context.statusTimer);
      context.statusTimer = null;
    }
    if (context.diagnosticsRetryTimer) {
      globalThis.clearTimeout(context.diagnosticsRetryTimer);
      context.diagnosticsRetryTimer = null;
    }
    context.streamAbortController?.abort();
    context.streamAbortController = null;
  }

  _headers(additional = {}) {
    const headers = new Headers(additional);
    if (this.authKey) {
      headers.set("x-functions-key", this.authKey);
    }
    return headers;
  }

  _assertDeploymentUrl(url) {
    const page = asHttpLocation(this.location);
    const root = deploymentRootFor(page);
    if (
      url.origin !== page.origin
      || !url.pathname.startsWith(root)
      || url.username
      || url.password
      || url.protocol !== page.protocol
    ) {
      throw new DurableChatProtocolError("The Durable Chat endpoint is outside this deployment.");
    }
  }
}

class DurableChatTransport {
  constructor(options) {
    this.bootstrap = options.bootstrap;
    this.deploymentRoot = options.deploymentRoot;
    this.fetch = options.fetch;
    this.getFunctionKey = options.getFunctionKey;
    this.location = options.location;
  }

  async start(body, idempotencyKey) {
    return this._json("start_run", {}, {
      body,
      headers: { "Idempotency-Key": idempotencyKey },
    });
  }

  async status(runId) {
    return this._json("status", { run_id: runId });
  }

  async result(runId) {
    return this._json("result", { run_id: runId });
  }

  async cancel(runId) {
    return this._json("cancel", { run_id: runId }, { body: {} });
  }

  async humanInputDetail(runId, requestId) {
    return this._json("human_input_detail", { run_id: runId, request_id: requestId });
  }

  async submitHumanInput(runId, requestId, answer, idempotencyKey) {
    return this._json("human_input_submit", { run_id: runId, request_id: requestId }, {
      body: { answer },
      headers: { "Idempotency-Key": idempotencyKey },
    });
  }

  async diagnostics(runId) {
    return this._json("diagnostics", { run_id: runId });
  }

  async events(runId, afterSequence, signal) {
    const response = await this._request("events", { run_id: runId }, {
      accept: "text/event-stream",
      headers: { "Last-Event-ID": String(Math.max(0, afterSequence)) },
      signal,
    });
    if (!response.ok) {
      throw new DurableChatHttpError(response.status, await readJsonResponse(response));
    }
    const contentType = response.headers.get("content-type") ?? "";
    if (!contentType.toLowerCase().startsWith("text/event-stream")) {
      throw new DurableChatProtocolError("The event endpoint did not return an SSE stream.");
    }
    return response;
  }

  async _json(name, values, options = {}) {
    const response = await this._request(name, values, {
      accept: "application/json",
      body: options.body,
      headers: options.headers,
      signal: options.signal,
    });
    const payload = await readJsonResponse(response);
    if (!response.ok) {
      throw new DurableChatHttpError(response.status, payload);
    }
    return { payload, status: response.status };
  }

  async _request(name, values, options = {}) {
    const descriptor = this.bootstrap.routes.get(name);
    if (!descriptor) {
      throw new DurableChatProtocolError("The requested Durable Chat route is unavailable.");
    }
    const url = resolveRoute(descriptor.pathTemplate, values, this.location, this.deploymentRoot);
    const headers = new Headers(options.headers ?? {});
    headers.set("Accept", options.accept ?? "application/json");
    const key = this.getFunctionKey();
    if (key) {
      headers.set("x-functions-key", key);
    }
    const request = {
      cache: "no-store",
      credentials: "same-origin",
      headers,
      method: descriptor.method,
      referrerPolicy: "no-referrer",
      redirect: "error",
      signal: options.signal,
    };
    if (options.body !== undefined) {
      headers.set("Content-Type", "application/json");
      request.body = JSON.stringify(options.body);
    }
    return this.fetch(url.href, request);
  }
}

export function createDurableChatApplication(options = {}) {
  const shell = options.shell ?? createDurableChatShell(options.root ?? document);
  return shell ? new DurableChatApplication(shell, options) : null;
}

async function consumeSseResponse(response, onFrame) {
  if (!response.body) {
    throw new DurableChatProtocolError("The event stream has no readable body.");
  }
  const reader = response.body.getReader();
  const parser = new DurableChatSseParser();
  try {
    while (true) {
      const chunk = await reader.read();
      if (chunk.done) {
        break;
      }
      for (const frame of parser.push(chunk.value)) {
        await onFrame(frame);
      }
    }
    for (const frame of parser.finish()) {
      await onFrame(frame);
    }
  } finally {
    reader.releaseLock();
  }
}

function parseSsePayload(frame) {
  if (!frame.data) {
    throw new DurableChatProtocolError("The event stream frame has no data.");
  }
  let payload;
  try {
    payload = JSON.parse(frame.data);
  } catch {
    throw new DurableChatProtocolError("The event stream frame is not JSON.");
  }
  if (!isObject(payload) || payload.schema_version !== "1") {
    throw new DurableChatProtocolError("The event stream frame has an invalid schema version.");
  }
  if (payload.event_type === "snapshot") {
    if (!isObject(payload.projection)) {
      throw new DurableChatProtocolError("The event stream snapshot is invalid.");
    }
    if (frame.event !== "snapshot") {
      throw new DurableChatProtocolError("The SSE event name does not match its payload.");
    }
    assertSseId(frame.id, payload.projection.through_sequence);
    return { kind: "snapshot", snapshot: payload };
  }
  if (!Number.isSafeInteger(payload.sequence) || payload.sequence <= 0 || !isObject(payload.event)) {
    throw new DurableChatProtocolError("The event stream delta is invalid.");
  }
  if (frame.event !== payload.event.event_type) {
    throw new DurableChatProtocolError("The SSE event name does not match its payload.");
  }
  assertSseId(frame.id, payload.sequence);
  return { kind: "event", frame: payload };
}

function assertSseId(id, expectedSequence) {
  if (id === "") {
    return;
  }
  if (!/^(?:0|[1-9][0-9]*)$/.test(id) || Number(id) !== expectedSequence) {
    throw new DurableChatProtocolError("The SSE event ID does not match its sequence.");
  }
}

function emptySseEvent() {
  return { data: [], event: "", id: "" };
}

function hasSseEventData(event) {
  return event.data.length > 0;
}

function applyProgress(projection, progress) {
  if (!isObject(progress)) {
    return;
  }
  projection.status = uiStatus(progress.status);
  projection.phase = stringOr(progress.phase, projection.phase);
  projection.progress = normalizeProgress(progress);
  projection.resultAvailable = progress.result_available === true;
  if (projection.status !== "waiting") {
    projection.humanInput = null;
  }
  clearDraftForNonSuccessTerminal(projection);
}

function applyModelAttempt(projection, event) {
  if (
    !isObject(event.producer)
    || !Number.isInteger(event.producer.step_index)
    || !Number.isInteger(event.producer.observation_epoch)
    || hasTerminalRunState(projection)
  ) {
    return;
  }
  const producer = event.producer;
  const attempts = isObject(projection.modelAttempts) ? projection.modelAttempts : {};
  const existing = attempts[String(producer.step_index)];
  if (
    nonnegativeInteger(existing?.epoch) > producer.observation_epoch
    || (
      nonnegativeInteger(existing?.epoch) === producer.observation_epoch
      && isTerminalModelAttemptState(existing?.state)
      && !isTerminalModelAttemptState(event.state)
    )
  ) {
    return;
  }
  setProducerEpoch(projection, producer);
  attempts[String(producer.step_index)] = {
    epoch: producer.observation_epoch,
    state: stringOr(event.state, "started"),
    updatedAt: stringOr(event.observed_at, isoNow()),
  };
  projection.modelAttempts = attempts;
  if (["failed", "superseded"].includes(event.state) && sameModelProducer(projection.draft?.producer, producer)) {
    projection.draft = null;
  }
}

function applyDraftReplacement(projection, event) {
  if (!isObject(event.producer) || !isObject(event.previous_producer)) {
    return;
  }
  setProducerEpoch(projection, event.producer);
  const draftProducer = projection.draft?.producer;
  if (
    sameModelProducer(draftProducer, event.previous_producer)
    || (
      isObject(draftProducer)
      && draftProducer.step_index === event.producer.step_index
      && draftProducer.observation_epoch < event.producer.observation_epoch
    )
  ) {
    projection.draft = null;
  }
}

function applyToolProgress(projection, progress) {
  if (!isObject(progress) || !isObject(progress.producer) || !nonEmptyString(progress.producer.call_key)) {
    return;
  }
  const incoming = normalizeToolProgress(progress);
  if (hasTerminalRunState(projection) && !isTerminalToolProgressState(incoming.state)) {
    return;
  }
  projection.toolProgress = mergeToolProgress(projection.toolProgress, [incoming]);
}

function applyHumanInputObservation(projection, event) {
  if (!nonEmptyString(event.request_id)) {
    return;
  }
  const prior = projection.humanInput?.requestId === event.request_id
    ? projection.humanInput
    : {};
  projection.humanInput = {
    ...prior,
    expiresAt: stringOr(event.expires_at, ""),
    requestId: event.request_id,
    state: stringOr(event.state, "pending"),
  };
  if (event.state === "pending") {
    projection.status = "waiting";
  }
}

function applyAuthoritativeStatus(projection, status) {
  projection.status = uiStatus(status.status);
  projection.phase = stringOr(status.phase, projection.phase);
  projection.possiblyCommitted = status.possibly_committed === true;
  projection.resultAvailable = status.result_available === true || projection.resultAvailable === true;
  if (typeof status.error === "string") {
    projection.error = displayError(status.error, "The durable run reported an error.");
  } else if (projection.status !== "uncertain") {
    projection.error = null;
  }
  if (isObject(status.human_input) && nonEmptyString(status.human_input.request_id)) {
    const previous = projection.humanInput?.requestId === status.human_input.request_id
      ? projection.humanInput
      : {};
    projection.humanInput = {
      ...previous,
      allowFreeText: status.human_input.allow_free_text === true,
      choiceCount: nonnegativeInteger(status.human_input.choice_count) ?? 0,
      expiresAt: stringOr(status.human_input.expires_at, ""),
      requestId: status.human_input.request_id,
      schemaPresent: status.human_input.schema_present === true,
      state: previous.state === "answered" ? "answered" : "pending",
    };
  } else if (projection.status !== "waiting") {
    projection.humanInput = null;
  }
  if (projection.status === "completed") {
    projection.completionPending = !nonEmptyString(projection.finalResponse);
  }
  clearDraftForNonSuccessTerminal(projection);
}

function clearDraftForNonSuccessTerminal(projection, status = projection.status) {
  if (isNonSuccessTerminalStatus(status)) {
    projection.draft = null;
  }
}

function isNonSuccessTerminalStatus(status) {
  return ["failed", "cancelled", "expired"].includes(uiStatus(status));
}

function hasTerminalSignal(projection) {
  return isObject(projection.terminalSignal);
}

function hasTerminalRunState(projection) {
  return hasTerminalSignal(projection) || isTerminalRunStatus(projection.status);
}

function isTerminalRunStatus(status) {
  return ["completed", "failed", "cancelled", "expired"].includes(uiStatus(status));
}

function projectionFromSnapshot(current, server, foregroundStreaming) {
  const preservePendingCompletion = current?.completionPending === true
    && viewStatus(current?.status) === "completed"
    && !nonEmptyString(current?.finalResponse);
  const next = createInitialProjection({
    foregroundStreaming,
    runId: stringOr(server.run_id, ""),
    sessionId: stringOr(server.session_id, stringOr(current?.sessionId, "")),
    status: uiStatus(server.progress?.status),
  });
  next.startAttempts = startAttempts(current);
  next.startState = "accepted";
  next.finalResponse = stringOr(current?.finalResponse, "");
  next.terminalConfirmed = current?.terminalConfirmed === true;
  next.terminalSignal = hasTerminalSignal(current) ? clone(current.terminalSignal) : null;
  next.cancelRequested = current?.cancelRequested === true;
  next.diagnostics = isObject(current?.diagnostics) ? clone(current.diagnostics) : null;
  next.streamState = "connected";
  applyProgress(next, server.progress);
  if (
    next.status === "waiting"
    && isObject(current?.humanInput)
    && ["pending", "answered"].includes(current.humanInput.state)
  ) {
    next.humanInput = clone(current.humanInput);
  }
  next.producerEpochs = producerEpochMap(server.producer_epochs, current?.producerEpochs);
  next.draft = preferredSnapshotDraft(current?.draft, server.draft, next.producerEpochs);
  next.toolProgress = mergeToolProgress(current?.toolProgress, server.tool_progress);
  next.sandboxObservations = mergeSandboxObservations(
    Array.isArray(current?.sandboxObservations) ? current.sandboxObservations : [],
    Array.isArray(server.sandbox_observations) ? server.sandbox_observations : [],
  );
  next.observationHealth = normalizeObservationHealth(server.observation_health);
  const publishedRevision = positiveInteger(server.published_revision);
  if (publishedRevision !== null) {
    next.serverPublishedRevision = publishedRevision;
  }
  next.serverThroughSequence = nonnegativeInteger(server.through_sequence) ?? 0;
  if (preservePendingCompletion) {
    next.status = "completed";
    next.completionPending = true;
    next.error = isObject(current?.error) ? clone(current.error) : null;
    next.terminalSignal = isObject(current?.terminalSignal) ? clone(current.terminalSignal) : null;
  } else {
    next.completionPending = next.status === "completed" && !next.finalResponse;
  }
  clearDraftForNonSuccessTerminal(next);
  clearDraftForNonSuccessTerminal(next, next.terminalSignal?.status);
  return next;
}

function projectionFromStored(stored, sessionId, foregroundStreaming) {
  if (!isObject(stored)) {
    return createInitialProjection({ foregroundStreaming, sessionId });
  }
  const projection = clone(stored);
  projection.sessionId = stringOr(projection.sessionId, sessionId);
  if (typeof projection.foregroundStreaming !== "boolean") {
    projection.foregroundStreaming = foregroundStreaming === true;
  }
  projection.status = viewStatus(projection);
  projection.terminalConfirmed = projection.terminalConfirmed === true;
  if (projection.terminalConfirmed) {
    projection.terminalSignal = null;
  }
  projection.startAttempts = startAttempts(projection);
  projection.producerEpochs = isObject(projection.producerEpochs)
    ? projection.producerEpochs
    : {};
  projection.toolProgress = Array.isArray(projection.toolProgress) ? projection.toolProgress : [];
  projection.sandboxObservations = Array.isArray(projection.sandboxObservations)
    ? projection.sandboxObservations
    : [];
  projection.observationHealth = normalizeObservationHealth(projection.observationHealth);
  clearDraftForNonSuccessTerminal(projection);
  clearDraftForNonSuccessTerminal(projection, projection.terminalSignal?.status);
  return projection;
}

function createInitialProjection(options = {}) {
  return {
    cancelRequested: false,
    completionPending: false,
    diagnostics: null,
    draft: null,
    error: null,
    finalResponse: "",
    foregroundStreaming: options.foregroundStreaming === true,
    humanInput: null,
    observationHealth: { degraded: false, reasons: [] },
    phase: "",
    producerEpochs: {},
    progress: null,
    resultAvailable: false,
    runId: stringOr(options.runId, ""),
    sandboxObservations: [],
    sessionId: stringOr(options.sessionId, ""),
    startAttempts: 0,
    startState: "pending",
    status: options.status ?? "submitting",
    streamState: "idle",
    terminalConfirmed: false,
    terminalSignal: null,
    toolProgress: [],
    updatedAt: isoNow(),
  };
}

function cursorFromSnapshot(snapshot) {
  const projection = snapshot.projection;
  const position = nonnegativeInteger(projection.through_sequence);
  const revision = positiveInteger(projection.published_revision);
  if (position === null || revision === null) {
    throw new DurableChatProtocolError("The snapshot cursor is invalid.");
  }
  return {
    position,
    revision,
    value: {
      after_sequence: position,
      published_revision: revision,
      run_id: projection.run_id,
    },
  };
}

function cursorFromEvent(frame, publishedRevision) {
  return {
    position: frame.sequence,
    revision: publishedRevision ?? 0,
    value: {
      after_sequence: frame.sequence,
      run_id: frame.event.run_id,
      ...(publishedRevision === null
        ? {}
        : { published_revision: publishedRevision }),
    },
  };
}

function optionalEventPublishedRevision(frame) {
  if (!Object.hasOwn(frame, "published_revision") || frame.published_revision === null) {
    return null;
  }
  const revision = positiveInteger(frame.published_revision);
  if (revision === null) {
    throw new DurableChatProtocolError("The event stream revision is invalid.");
  }
  return revision;
}

function zeroCursor(runId) {
  return {
    position: 0,
    revision: 0,
    value: runId ? { after_sequence: 0, run_id: runId } : {},
  };
}

function normalizeDiagnostics(diagnostics) {
  const links = Array.isArray(diagnostics.links)
    ? diagnostics.links.filter(isObject).map((link) => ({
      available: link.available === true && Boolean(safeExternalUrl(link.href)),
      href: safeExternalUrl(link.href),
      kind: stringOr(link.kind, ""),
      unavailableReason: stringOr(link.unavailable_reason, ""),
    }))
    : [];
  return {
    configuredSandboxGroupResourceId: stringOr(
      diagnostics.configured_sandbox_group_resource_id,
      "",
    ),
    createdAt: stringOr(diagnostics.created_at, ""),
    expiresAt: stringOr(diagnostics.expires_at, ""),
    links,
    modelMode: stringOr(diagnostics.model_mode, ""),
    note: "",
    observationHealth: normalizeObservationHealth(diagnostics.observation_health),
    sandboxObservations: Array.isArray(diagnostics.sandbox_observations)
      ? mergeSandboxObservations([], diagnostics.sandbox_observations)
      : [],
    status: uiStatus(diagnostics.status),
    updatedAt: stringOr(diagnostics.updated_at, ""),
  };
}

function preserveExpiredDiagnostics(diagnostics) {
  const existing = isObject(diagnostics) ? clone(diagnostics) : {};
  const health = normalizeObservationHealth(existing.observationHealth);
  const expiryNote = "Request diagnostic observations have expired. Previously recorded request diagnostics remain available.";
  const note = stringOr(existing.note, "");
  return {
    ...existing,
    note: note.includes(expiryNote) ? note : [note, expiryNote].filter(Boolean).join(" "),
    observationHealth: {
      degraded: true,
      reasons: [...new Set([...health.reasons, "chat_observations_expired"])],
    },
  };
}

function humanInputView(human, details) {
  if (!isObject(human) || human.state !== "pending") {
    return undefined;
  }
  const responseSchema = details?.requestId === human.requestId ? details.responseSchema : null;
  const choices = details?.requestId === human.requestId
    ? details.choices
    : Array.isArray(human.choices) ? human.choices : [];
  const allowFreeText = details?.requestId === human.requestId
    ? details.allowFreeText
    : human.allowFreeText === true;
  const schema = responseSchema
    ? {
      description: stringOr(responseSchema.description, ""),
      label: stringOr(responseSchema.title, "Structured response"),
      value: JSON.stringify(responseSchema, null, 2),
    }
    : undefined;
  return {
    allowFreeText,
    choices: choices.map((choice) => ({ label: choice, value: choice })),
    id: human.requestId,
    prompt: stringOr(details?.question, stringOr(human.question, "Input is required to continue this request.")),
    schema,
    submitting: human.submitting === true,
  };
}

function progressView(projection) {
  const entries = [];
  if (isObject(projection.progress)) {
    entries.push({
      detail: progressSummary(projection.progress),
      label: phaseLabel(projection.phase),
      state: progressState(projection.status),
      timestamp: displayTimestamp(projection.progress.updatedAt),
    });
  }
  for (const attempt of Object.entries(isObject(projection.modelAttempts) ? projection.modelAttempts : {})) {
    const [step, value] = attempt;
    entries.push({
      detail: `Attempt ${stringOr(value.epoch, "")}`.trim(),
      label: `Model step ${step}`,
      state: modelAttemptProgressState(value.state),
      timestamp: displayTimestamp(value.updatedAt),
    });
  }
  for (const tool of Array.isArray(projection.toolProgress) ? projection.toolProgress : []) {
    entries.push({
      detail: humanizeIdentifier(tool.state),
      label: tool.toolName || "Tool call",
      state: toolProgressState(tool.state),
      timestamp: displayTimestamp(tool.updatedAt),
    });
  }
  if (isObject(projection.humanInput)) {
    entries.push({
      detail: projection.humanInput.state === "pending"
        ? "A response is required."
        : "A response was submitted.",
      label: "Human input",
      state: projection.humanInput.state === "pending" ? "waiting" : "completed",
      timestamp: displayTimestamp(projection.humanInput.expiresAt),
    });
  }
  if (isObject(projection.terminalSignal) && projection.terminalConfirmed !== true) {
    entries.push({
      detail: "Waiting for the authoritative status and result routes.",
      label: "Terminal event received",
      state: "running",
      timestamp: displayTimestamp(projection.terminalSignal.observedAt),
    });
  }
  return entries;
}

function sandboxView(observations, configuredSandboxGroupResourceId) {
  const history = Array.isArray(observations) ? observations : [];
  const ordered = [...history]
    .sort((left, right) => stringOr(left.observedAt, "").localeCompare(stringOr(right.observedAt, "")));
  const latest = ordered.at(-1);
  const observedGroup = [...ordered]
    .reverse()
    .map((observation) => stringOr(observation?.sandboxGroupResourceId, ""))
    .find(Boolean) ?? "";
  const configuredGroup = history.length === 0
    ? stringOr(configuredSandboxGroupResourceId, "")
    : "";
  return {
    group: observedGroup || configuredGroup,
    groupSource: observedGroup ? "observation" : configuredGroup ? "configured" : "",
    history: history.map((observation) => ({
      detail: [
        observation.sandboxId ? `Sandbox ${observation.sandboxId}` : "",
        Number.isInteger(observation.sandboxGeneration)
          ? `Generation ${observation.sandboxGeneration}`
          : "",
        observation.replacedSandboxId ? `Replaced ${observation.replacedSandboxId}` : "",
      ].filter(Boolean).join(" · "),
      label: [
        observation.toolName || "Tool",
        humanizeIdentifier(observation.state),
      ].filter(Boolean).join(" · "),
    })),
    id: stringOr(latest?.sandboxId, ""),
  };
}

function diagnosticsView(diagnostics) {
  if (!isObject(diagnostics)) {
    return {
      appInsightsReason: "Diagnostic links are not yet available for this request.",
      dtsReason: "Diagnostic links are not yet available for this request.",
      note: "Per-request diagnostic links appear when the Function App reports them.",
    };
  }
  const dts = diagnostics.links?.find((link) => link.kind === "durable_task_scheduler");
  const appInsights = diagnostics.links?.find((link) => link.kind === "application_insights");
  return {
    appInsightsReason: stringOr(appInsights?.unavailableReason, "Application Insights is not available for this request."),
    appInsightsUrl: appInsights?.available ? appInsights.href : "",
    dtsReason: stringOr(dts?.unavailableReason, "DTS is not available for this request."),
    dtsUrl: dts?.available ? dts.href : "",
    note: stringOr(
      diagnostics.note,
      diagnostics.modelMode === "background"
        ? "Background model mode reports durable progress; final response text is available after completion."
        : appInsights?.available
          ? "Application Insights data may take time to appear."
          : "",
    ),
  };
}

function transcriptFor(record, projection) {
  const transcript = Array.isArray(record.transcript) ? clone(record.transcript) : [];
  if (!transcript.some((entry) => entry?.role === "user")) {
    transcript.unshift({
      role: "user",
      submittedAt: record.createdAt,
      text: stringOr(record.normalizedSubmission?.prompt, ""),
    });
  }
  const finalResponse = stringOr(projection.finalResponse, "");
  const withoutFinal = transcript.filter((entry) => entry?.kind !== "final_response");
  if (finalResponse) {
    withoutFinal.push({
      kind: "final_response",
      receivedAt: isoNow(),
      role: "assistant",
      text: finalResponse,
    });
  }
  return withoutFinal;
}

function mergeSandboxObservations(existing, incoming) {
  const merged = [];
  const keys = new Set();
  for (const source of [...existing, ...incoming]) {
    const observation = normalizeSandboxObservation(source);
    if (!observation) {
      continue;
    }
    const key = [
      observation.callKey,
      observation.sandboxId,
      observation.sandboxGeneration,
      observation.state,
      observation.observedAt,
    ].join("|");
    if (!keys.has(key)) {
      keys.add(key);
      merged.push(observation);
    }
  }
  return merged;
}

function normalizeSandboxObservation(observation) {
  if (!isObject(observation)) {
    return null;
  }
  const producer = isObject(observation.producer) ? observation.producer : {};
  const callKey = stringOr(producer.call_key, stringOr(observation.callKey, ""));
  return {
    callKey,
    observedAt: stringOr(observation.observed_at, stringOr(observation.observedAt, "")),
    provenance: stringOr(observation.provenance, ""),
    replacedSandboxId: stringOr(observation.replaced_sandbox_id, stringOr(observation.replacedSandboxId, "")),
    sandboxGeneration: nonnegativeInteger(
      observation.sandbox_generation ?? observation.sandboxGeneration,
    ),
    sandboxGroupResourceId: stringOr(
      observation.sandbox_group_resource_id,
      stringOr(observation.sandboxGroupResourceId, ""),
    ),
    sandboxId: stringOr(observation.sandbox_id, stringOr(observation.sandboxId, "")),
    sandboxProfile: stringOr(observation.sandbox_profile, stringOr(observation.sandboxProfile, "")),
    state: stringOr(observation.state, ""),
    stepIndex: nonnegativeInteger(observation.step_index ?? observation.stepIndex),
    toolName: stringOr(observation.tool_name, stringOr(observation.toolName, "")),
  };
}

function normalizeProgress(progress) {
  if (!isObject(progress)) {
    return null;
  }
  return {
    humanWaits: nonnegativeInteger(progress.human_waits) ?? 0,
    inputTokens: nonnegativeInteger(progress.input_tokens) ?? 0,
    modelSteps: nonnegativeInteger(progress.model_steps) ?? 0,
    outputTokens: nonnegativeInteger(progress.output_tokens) ?? 0,
    phase: stringOr(progress.phase, ""),
    resultAvailable: progress.result_available === true,
    status: uiStatus(progress.status),
    stepIndex: nonnegativeInteger(progress.step_index) ?? 0,
    toolCalls: nonnegativeInteger(progress.tool_calls) ?? 0,
    updatedAt: stringOr(progress.updated_at, ""),
  };
}

function normalizeToolProgress(progress) {
  return {
    producer: {
      call_key: stringOr(progress?.producer?.call_key, ""),
    },
    state: stringOr(progress?.state, ""),
    stepIndex: nonnegativeInteger(progress?.step_index ?? progress?.stepIndex) ?? 0,
    toolName: stringOr(progress?.tool_name ?? progress?.toolName, ""),
    updatedAt: stringOr(progress?.updated_at ?? progress?.updatedAt, ""),
  };
}

function mergeToolProgress(current, updates) {
  const merged = [];
  for (const progress of [...(Array.isArray(current) ? current : []), ...(Array.isArray(updates) ? updates : [])]) {
    const incoming = normalizeToolProgress(progress);
    if (!nonEmptyString(incoming.producer.call_key)) {
      continue;
    }
    const index = merged.findIndex((entry) => entry.producer.call_key === incoming.producer.call_key);
    if (index === -1) {
      merged.push(incoming);
    } else if (shouldReplaceToolProgress(merged[index], incoming)) {
      merged[index] = incoming;
    }
  }
  return merged;
}

function shouldReplaceToolProgress(current, incoming) {
  const currentTerminal = isTerminalToolProgressState(current.state);
  const incomingTerminal = isTerminalToolProgressState(incoming.state);
  if (currentTerminal !== incomingTerminal) {
    return incomingTerminal;
  }
  return !current.updatedAt || !incoming.updatedAt || incoming.updatedAt >= current.updatedAt;
}

function isTerminalToolProgressState(state) {
  return ["succeeded", "failed", "timed_out", "cancelled", "ambiguous"].includes(state);
}

function normalizeDraft(draft) {
  if (!isObject(draft) || !isObject(draft.producer) || typeof draft.text !== "string") {
    return null;
  }
  return {
    producer: clone(draft.producer),
    text: draft.text,
    updatedAt: stringOr(draft.updated_at ?? draft.updatedAt, ""),
  };
}

function preferredSnapshotDraft(current, snapshot, producerEpochs) {
  const snapshotDraft = normalizeDraft(snapshot);
  if (!snapshotDraft) {
    return null;
  }
  const currentDraft = normalizeDraft(current);
  if (
    isCurrentModelProducerForEpochs(snapshotDraft.producer, producerEpochs)
    && !isOlderModelProducer(snapshotDraft.producer, currentDraft?.producer)
  ) {
    return snapshotDraft;
  }
  return currentDraft;
}

function normalizeObservationHealth(value) {
  return {
    degraded: value?.degraded === true,
    reasons: Array.isArray(value?.reasons)
      ? value.reasons.filter((reason) => typeof reason === "string")
      : [],
  };
}

function producerEpochMap(producerEpochs, current) {
  const mapped = isObject(current) ? clone(current) : {};
  if (!Array.isArray(producerEpochs)) {
    return mapped;
  }
  for (const producer of producerEpochs) {
    if (isObject(producer) && Number.isInteger(producer.step_index) && Number.isInteger(producer.observation_epoch)) {
      mapped[String(producer.step_index)] = producer.observation_epoch;
    }
  }
  return mapped;
}

function setProducerEpoch(projection, producer) {
  const epochs = isObject(projection.producerEpochs) ? projection.producerEpochs : {};
  const step = String(producer.step_index);
  const existing = nonnegativeInteger(epochs[step]) ?? 0;
  if (producer.observation_epoch > existing) {
    epochs[step] = producer.observation_epoch;
  }
  projection.producerEpochs = epochs;
}

function isCurrentModelProducer(projection, producer) {
  if (hasTerminalSignal(projection) || !isCurrentModelProducerForEpochs(producer, projection.producerEpochs)) {
    return false;
  }
  setProducerEpoch(projection, producer);
  return true;
}

function isCurrentModelProducerForEpochs(producer, producerEpochs) {
  if (!isObject(producer) || !Number.isInteger(producer.step_index) || !Number.isInteger(producer.observation_epoch)) {
    return false;
  }
  const epochs = isObject(producerEpochs) ? producerEpochs : {};
  const known = nonnegativeInteger(epochs[String(producer.step_index)]);
  if (known !== null && producer.observation_epoch < known) {
    return false;
  }
  const latestStep = Math.max(
    -1,
    ...Object.keys(epochs)
      .map(Number)
      .filter((step) => nonnegativeInteger(step) !== null),
  );
  return producer.step_index >= latestStep;
}

function isOlderModelProducer(left, right) {
  return isObject(left)
    && isObject(right)
    && Number.isInteger(left.step_index)
    && Number.isInteger(left.observation_epoch)
    && Number.isInteger(right.step_index)
    && Number.isInteger(right.observation_epoch)
    && (
      left.step_index < right.step_index
      || (
        left.step_index === right.step_index
        && left.observation_epoch < right.observation_epoch
      )
    );
}

function isTerminalModelAttemptState(state) {
  return ["completed", "failed", "superseded"].includes(state);
}

function sameModelProducer(left, right) {
  return isObject(left)
    && isObject(right)
    && left.producer_type === "model"
    && right.producer_type === "model"
    && left.step_index === right.step_index
    && left.observation_epoch === right.observation_epoch;
}

function normalizeHumanAnswer(context, detail) {
  if (detail.responseType === "choice") {
    if (typeof detail.response !== "string") {
      throw new Error("Choose one of the available responses.");
    }
    return detail.response;
  }
  if (typeof detail.response !== "string" || !detail.response.trim()) {
    throw new Error("Enter a response before submitting.");
  }
  if (context.humanDetails?.responseSchema) {
    try {
      return JSON.parse(detail.response);
    } catch {
      throw new Error("Enter valid JSON that matches the requested response schema.");
    }
  }
  return detail.response;
}

function resolveRoute(pathTemplate, values, locationLike, deploymentRoot) {
  const page = asHttpLocation(locationLike);
  let path = pathTemplate;
  path = path.replace(ROUTE_PARAMETER_PATTERN, (match, name) => {
    const value = values[name];
    if (!nonEmptyString(value)) {
      throw new DurableChatProtocolError(`The Durable Chat route requires ${name}.`);
    }
    return encodeURIComponent(value);
  });
  if (/[{}]/.test(path)) {
    throw new DurableChatProtocolError("The Durable Chat route has unresolved parameters.");
  }
  const url = new URL(path, page.origin);
  if (
    url.origin !== page.origin
    || url.protocol !== page.protocol
    || !url.pathname.startsWith(deploymentRoot)
    || url.search
    || url.hash
    || url.username
    || url.password
  ) {
    throw new DurableChatProtocolError("The Durable Chat route is outside this deployment.");
  }
  return url;
}

function validateRouteTemplate(template, expectedParameters, page, deploymentRoot) {
  if (
    typeof template !== "string"
    || !template.startsWith("/")
    || template.startsWith("//")
    || template.includes("\\")
    || template.includes("?")
    || template.includes("#")
    || template.includes("://")
    || /[\u0000-\u001f\s]/.test(template)
  ) {
    throw new DurableChatProtocolError("The Durable Chat route template is unsafe.");
  }
  let decoded;
  try {
    decoded = decodeURIComponent(template);
  } catch {
    throw new DurableChatProtocolError("The Durable Chat route template is malformed.");
  }
  if (
    decoded.includes("\\")
    || decoded.includes("//")
    || decoded.includes("://")
    || /[\u0000-\u001f\s]/.test(decoded)
    || decoded.split("/").includes("..")
  ) {
    throw new DurableChatProtocolError("The Durable Chat route template is unsafe.");
  }
  const parameters = Array.from(template.matchAll(ROUTE_PARAMETER_PATTERN), (match) => match[1]).sort();
  const expected = [...expectedParameters].sort();
  if (
    parameters.length !== expected.length
    || parameters.some((parameter, index) => parameter !== expected[index])
    || /[{}]/.test(template.replace(ROUTE_PARAMETER_PATTERN, ""))
  ) {
    throw new DurableChatProtocolError("The Durable Chat route template has invalid parameters.");
  }
  resolveRoute(template, Object.fromEntries(expected.map((name) => [name, "route-parameter"])), page, deploymentRoot);
  return template;
}

function deploymentRootFor(page) {
  const marker = "/experimental/durable-chat";
  const index = page.pathname.indexOf(marker);
  if (index >= 0) {
    return page.pathname.slice(0, index + 1) || "/";
  }
  const base = new URL("./", page);
  return base.pathname.endsWith("/") ? base.pathname : `${base.pathname}/`;
}

function asHttpLocation(locationLike) {
  const value = locationLike?.href ?? locationLike;
  let url;
  try {
    url = new URL(value);
  } catch {
    throw new DurableChatProtocolError("Durable Chat requires an HTTP(S) page location.");
  }
  if (!["http:", "https:"].includes(url.protocol) || !url.origin || url.origin === "null") {
    throw new DurableChatProtocolError("Durable Chat requires an HTTP(S) page location.");
  }
  return url;
}

function sandboxProfileOption(value) {
  return {
    label: value === "retained_session" ? "Retained sandbox" : "Per-call sandbox",
    value,
  };
}

function isSandboxProfile(value) {
  return value === "per_call" || value === "retained_session";
}

function uiStatus(value) {
  switch (value) {
    case "Pending":
    case "pending":
      return "queued";
    case "Running":
    case "running":
      return "running";
    case "Waiting":
    case "waiting":
      return "waiting";
    case "Completed":
    case "completed":
      return "completed";
    case "Failed":
    case "failed":
      return "failed";
    case "Cancelled":
    case "cancelled":
      return "cancelled";
    case "submitting":
    case "session_busy":
    case "uncertain":
    case "expired":
    case "reconnecting":
    case "degraded":
    case "cancel_requested":
      return value;
    default:
      return "unknown";
  }
}

function viewStatus(projection) {
  if (!isObject(projection)) {
    return "unknown";
  }
  if (projection.status === "session_busy" || projection.status === "expired") {
    return projection.status;
  }
  if (projection.cancelRequested === true && !projection.terminalConfirmed) {
    return "cancel_requested";
  }
  if (!nonEmptyString(projection.runId) && isStartUncertain(projection)) {
    return "uncertain";
  }
  if (!projection.terminalConfirmed && projection.streamState === "reconnecting") {
    return "reconnecting";
  }
  if (!projection.terminalConfirmed && projection.observationHealth?.degraded === true) {
    return "degraded";
  }
  return uiStatus(projection.status);
}

function isStartUncertain(projection) {
  return projection?.startState === "uncertain" || projection?.startState === "pending";
}

function startAttempts(projection) {
  return nonnegativeInteger(projection?.startAttempts) ?? 0;
}

function displayError(code, message) {
  return {
    code: safeErrorCode(code),
    message,
  };
}

function errorCode(payload) {
  return isObject(payload) && typeof payload.error === "string" ? safeErrorCode(payload.error) : "";
}

function safeErrorCode(value) {
  return typeof value === "string" && /^[a-z][a-z0-9_.-]{0,127}$/i.test(value)
    ? value
    : "request_error";
}

function progressSummary(progress) {
  const parts = [];
  if (Number.isInteger(progress.modelSteps)) {
    parts.push(`${progress.modelSteps} model step${progress.modelSteps === 1 ? "" : "s"}`);
  }
  if (Number.isInteger(progress.toolCalls)) {
    parts.push(`${progress.toolCalls} tool call${progress.toolCalls === 1 ? "" : "s"}`);
  }
  return parts.join(" · ");
}

function phaseLabel(value) {
  return value ? humanizeIdentifier(value) : "Durable run";
}

function progressState(status) {
  if (status === "completed") {
    return "completed";
  }
  if (status === "waiting") {
    return "waiting";
  }
  if (["failed", "cancelled", "expired"].includes(status)) {
    return "failed";
  }
  return "running";
}

function modelAttemptProgressState(state) {
  return ["completed"].includes(state)
    ? "completed"
    : ["failed", "superseded"].includes(state)
      ? "failed"
      : "running";
}

function toolProgressState(state) {
  return state === "succeeded"
    ? "completed"
    : ["failed", "timed_out", "cancelled", "ambiguous"].includes(state)
      ? "failed"
      : "running";
}

function displayTimestamp(value) {
  if (typeof value !== "string" || !value) {
    return "";
  }
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
  });
}

function humanizeIdentifier(value) {
  return typeof value === "string" && value
    ? value.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase())
    : "";
}

function compareUpdatedSessions(left, right) {
  return stringOr(right.updatedAt, "").localeCompare(stringOr(left.updatedAt, ""))
    || stringOr(left.sessionId, "").localeCompare(stringOr(right.sessionId, ""));
}

function compareRequests(left, right) {
  return stringOr(left.createdAt, "").localeCompare(stringOr(right.createdAt, ""))
    || stringOr(left.requestId, "").localeCompare(stringOr(right.requestId, ""));
}

function contextKey(sessionId, requestId) {
  return `${sessionId}\u0000${requestId}`;
}

function selectedRequestStorageKey(namespace, sessionId) {
  return `${SELECTED_REQUEST_STORAGE_PREFIX}${namespace}.${sessionId}`;
}

function createOpaqueIdentifier(prefix) {
  if (globalThis.crypto?.randomUUID) {
    return `${prefix}-${globalThis.crypto.randomUUID()}`;
  }
  if (globalThis.crypto?.getRandomValues) {
    const bytes = new Uint8Array(16);
    globalThis.crypto.getRandomValues(bytes);
    return `${prefix}-${Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("")}`;
  }
  throw new Error("A cryptographic random source is unavailable.");
}

async function readJsonResponse(response) {
  const text = await response.text();
  if (!text) {
    return {};
  }
  try {
    const payload = JSON.parse(text);
    return isObject(payload) ? payload : {};
  } catch {
    return {};
  }
}

function safeExternalUrl(value) {
  if (!nonEmptyString(value)) {
    return "";
  }
  let url;
  try {
    url = new URL(value);
  } catch {
    return "";
  }
  const localHttp = url.protocol === "http:"
    && ["localhost", "127.0.0.1", "[::1]", "::1"].includes(url.hostname);
  if (
    (!localHttp && url.protocol !== "https:")
    || url.username
    || url.password
    || hasSensitiveDiagnosticUrlMaterial(url)
  ) {
    return "";
  }
  return url.href;
}

function hasSensitiveDiagnosticUrlMaterial(url) {
  const fragment = url.hash.startsWith("#") ? url.hash.slice(1) : url.hash;
  const fragmentQuery = fragment.includes("?")
    ? fragment.slice(fragment.indexOf("?") + 1)
    : fragment;
  for (const component of [url.search, fragmentQuery]) {
    for (const [key, queryValue] of new URLSearchParams(component)) {
      const compact = key.replaceAll("-", "").replaceAll("_", "").toLowerCase();
      if (SENSITIVE_URL_KEYS.has(key.toLowerCase()) || SENSITIVE_URL_KEYS.has(compact)) {
        return true;
      }
      const lowered = queryValue.toLowerCase();
      if (["accountkey=", "sharedaccesssignature=", "sig=", "token=", "x-functions-key="].some(
        (marker) => lowered.includes(marker),
      )) {
        return true;
      }
    }
  }
  return false;
}

function delay(milliseconds) {
  return new Promise((resolve) => {
    globalThis.setTimeout(resolve, milliseconds);
  });
}

function isAbortError(error) {
  return error?.name === "AbortError";
}

function isoNow() {
  return new Date().toISOString();
}

function utf8ByteLength(value) {
  return new TextEncoder().encode(value).byteLength;
}

function nonnegativeInteger(value) {
  return Number.isSafeInteger(value) && value >= 0 ? value : null;
}

function positiveInteger(value) {
  return Number.isSafeInteger(value) && value > 0 ? value : null;
}

function nonEmptyString(value) {
  return typeof value === "string" && value.length > 0;
}

function validDisplayName(value) {
  return typeof value === "string" && value.length <= 160 && Boolean(value.trim());
}

function stringOr(value, fallback = "") {
  return typeof value === "string" ? value : fallback;
}

function isObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function clone(value) {
  return structuredClone(value);
}

if (typeof document !== "undefined") {
  const start = () => {
    const application = createDurableChatApplication();
    if (application) {
      void application.start();
    }
  };
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start, { once: true });
  } else {
    start();
  }
}
