const DATABASE_PREFIX = "azure-functions-agents.durable-chat.history.";
const DATABASE_VERSION = 1;
const SESSION_STORE = "sessions";
const REQUEST_STORE = "requests";
const MAX_IDENTIFIER_LENGTH = 512;
const SENSITIVE_FIELD_NAMES = new Set([
  "authorization",
  "proxyauthorization",
  "cookie",
  "setcookie",
  "headers",
  "credentials",
  "functionkey",
  "functionskey",
  "xfunctionkey",
  "xfunctionskey",
  "accesskey",
  "apikey",
  "xapikey",
  "accesstoken",
  "idtoken",
  "refreshtoken",
  "authtoken",
  "bearertoken",
  "clientsecret",
  "password",
]);

/**
 * Versioned browser-local storage for the Durable Agent Chat UI.
 *
 * The caller must provide `namespace` after authenticated bootstrap. It must
 * already scope deployment/route, agent, and an opaque owner value; this
 * module never derives it from a URL, function key, header, or browser user.
 *
 * All request fields are internal browser-record fields. The caller maps
 * transport payloads into these records rather than passing HTTP wire objects.
 *
 * @typedef {object} DurableChatHistoryCursor
 * @property {number} position A monotonically increasing local comparison value.
 * @property {number} [revision=0] Breaks ties at one position.
 * @property {unknown} [value] Opaque cursor data retained without wire assumptions.
 *
 * @typedef {object} DurableChatHistoryRequest
 * @property {string} sessionId
 * @property {string} requestId
 * @property {string} idempotencyKey
 * @property {unknown} normalizedSubmission
 * @property {unknown[]} transcript
 * @property {unknown|null} draft
 * @property {unknown|null} projection
 * @property {DurableChatHistoryCursor|null} cursor
 * @property {boolean} terminal
 * @property {number} version
 * @property {string} createdAt
 * @property {string} updatedAt
 *
 * @typedef {object} DurableChatHistorySession
 * @property {string} sessionId
 * @property {string} title
 * @property {number} version
 * @property {string} createdAt
 * @property {string} updatedAt
 */

/**
 * @typedef {object} HistoryMutationResult
 * @property {"persistent"|"volatile"} mode
 * @property {string} disposition
 * @property {DurableChatHistorySession} [session]
 * @property {DurableChatHistoryRequest} [request]
 * @property {number} [removedRequests]
 * @property {string} [sessionId]
 */

export const DURABLE_CHAT_HISTORY_SCHEMA_VERSION = DATABASE_VERSION;

export class DurableChatHistoryError extends Error {
  constructor(code, message, options = {}) {
    super(message);
    this.name = "DurableChatHistoryError";
    this.code = code;
    if (options.cause !== undefined) {
      this.cause = options.cause;
    }
  }
}

/**
 * Open browser-local durable-chat history.
 *
 * Persistent storage failures reject by default. A caller that deliberately
 * chooses volatile mode must provide `announceVolatileMode`; it is awaited
 * before a volatile history object is returned, so storage loss cannot look
 * like persistence success.
 *
 * @param {{
 *   namespace: string,
 *   allowVolatile?: boolean,
 *   announceVolatileMode?: (failure: {code: string, message: string}) => void|Promise<void>,
 * }} options
 * @returns {Promise<DurableChatHistory>}
 */
export async function openDurableChatHistory(options) {
  const namespace = requireIdentifier(options?.namespace, "namespace");
  const allowVolatile = options?.allowVolatile === true;
  const announceVolatileMode = options?.announceVolatileMode;

  try {
    const database = await openDatabase(databaseNameFor(namespace));
    return new PersistentDurableChatHistory(namespace, database);
  } catch (error) {
    const failure = toHistoryError(error, "storage_open_failed");
    if (
      !allowVolatile
      || failure.code === "unsupported_schema"
      || failure.code === "corrupt_database"
    ) {
      throw failure;
    }
    if (typeof announceVolatileMode !== "function") {
      throw new DurableChatHistoryError(
        "volatile_announcer_required",
        "Volatile history requires an announcement callback.",
        { cause: failure },
      );
    }

    try {
      await announceVolatileMode(publicFailure(failure));
    } catch (announcementError) {
      throw new DurableChatHistoryError(
        "volatile_announcement_failed",
        "Volatile history was not activated because its announcement failed.",
        { cause: announcementError },
      );
    }

    return new VolatileDurableChatHistory(namespace, publicFailure(failure));
  }
}

/**
 * @typedef {object} DurableChatHistory
 * @property {"persistent"|"volatile"} mode
 * @property {boolean} requiresAnnouncement
 * @property {(input: {sessionId: string, title?: string}) => Promise<HistoryMutationResult>} createSession
 * @property {(input: {sessionId: string, requestId: string, normalizedSubmission: unknown, idempotencyKey: string, title?: string, transcript?: unknown[], draft?: unknown, projection?: unknown}) => Promise<HistoryMutationResult>} recordSubmission
 * @property {(input: {sessionId: string, requestId: string, cursor: DurableChatHistoryCursor, projection: unknown, transcript?: unknown[], draft?: unknown, terminal?: boolean, expectedVersion?: number, mode?: "incremental"|"snapshot", authoritativeReset?: boolean}) => Promise<HistoryMutationResult>} applyProjection
 * @property {(input: {sessionId: string, title: string, expectedVersion: number}) => Promise<HistoryMutationResult>} renameSession
 * @property {(input: {sessionId: string, confirmed: true, expectedVersion: number}) => Promise<HistoryMutationResult>} removeSession
 * @property {() => Promise<{mode: "persistent"|"volatile", sessions: DurableChatHistorySession[]}>} listSessions
 * @property {(sessionId: string) => Promise<{mode: "persistent"|"volatile", session: DurableChatHistorySession|null}>} getSession
 * @property {(sessionId: string) => Promise<{mode: "persistent"|"volatile", requests: DurableChatHistoryRequest[]}>} listRequests
 * @property {(sessionId: string, requestId: string) => Promise<{mode: "persistent"|"volatile", request: DurableChatHistoryRequest|null}>} getRequest
 * @property {() => void} close
 */

class PersistentDurableChatHistory {
  constructor(namespace, database) {
    this.mode = "persistent";
    this.namespace = namespace;
    this.requiresAnnouncement = false;
    this._database = database;
    this._closed = false;
    this._database.onversionchange = () => {
      this._closed = true;
      this._database.close();
    };
  }

  async createSession(input) {
    const sessionInput = normalizeSessionInput(input);
    return this._write([SESSION_STORE], async ({ sessions }) => {
      const existing = await requestValue(sessions.get(sessionInput.sessionId));
      if (existing) {
        return mutationResult(this.mode, "existing", { session: copyForReturn(existing) });
      }

      const session = createSessionRecord(sessionInput, now());
      await requestValue(sessions.add(session));
      return mutationResult(this.mode, "created", { session: copyForReturn(session) });
    });
  }

  async recordSubmission(input) {
    const submission = normalizeSubmissionInput(input);
    return this._write([SESSION_STORE, REQUEST_STORE], async ({ sessions, requests }) => {
      const key = requestKey(submission.sessionId, submission.requestId);
      const existingRequest = await requestValue(requests.get(key));
      if (existingRequest) {
        if (sameSubmission(existingRequest, submission)) {
          return mutationResult(this.mode, "existing", {
            request: copyForReturn(existingRequest),
          });
        }
        return mutationResult(this.mode, "submission_conflict", {
          request: copyForReturn(existingRequest),
        });
      }

      const timestamp = now();
      const existingSession = await requestValue(sessions.get(submission.sessionId));
      const session = existingSession
        ? touchSession(existingSession, timestamp)
        : createSessionRecord(submission, timestamp);
      const request = createRequestRecord(submission, timestamp);

      await requestValue(sessions.put(session));
      await requestValue(requests.add(request));
      return mutationResult(this.mode, "recorded", {
        request: copyForReturn(request),
        session: copyForReturn(session),
      });
    });
  }

  async applyProjection(input) {
    const update = normalizeProjectionInput(input);
    return this._write([SESSION_STORE, REQUEST_STORE], async ({ sessions, requests }) => {
      const key = requestKey(update.sessionId, update.requestId);
      const existingRequest = await requestValue(requests.get(key));
      if (!existingRequest) {
        return mutationResult(this.mode, "missing");
      }

      const outcome = applyProjectionUpdate(existingRequest, update, now());
      if (!outcome.record) {
        return mutationResult(this.mode, outcome.disposition, {
          request: copyForReturn(existingRequest),
        });
      }

      const existingSession = await requestValue(sessions.get(update.sessionId));
      if (!existingSession) {
        throw new DurableChatHistoryError(
          "corrupt_database",
          "A request record has no matching local session record.",
        );
      }

      const session = touchSession(existingSession, outcome.record.updatedAt);
      await requestValue(requests.put(outcome.record));
      await requestValue(sessions.put(session));
      return mutationResult(this.mode, outcome.disposition, {
        request: copyForReturn(outcome.record),
        session: copyForReturn(session),
      });
    });
  }

  async renameSession(input) {
    const rename = normalizeRenameInput(input);
    return this._write([SESSION_STORE], async ({ sessions }) => {
      const existing = await requestValue(sessions.get(rename.sessionId));
      if (!existing) {
        return mutationResult(this.mode, "missing");
      }
      if (existing.version !== rename.expectedVersion) {
        return mutationResult(this.mode, "version_conflict", {
          session: copyForReturn(existing),
        });
      }

      const session = {
        ...existing,
        title: rename.title,
        updatedAt: now(),
        version: existing.version + 1,
      };
      await requestValue(sessions.put(session));
      return mutationResult(this.mode, "renamed", { session: copyForReturn(session) });
    });
  }

  async removeSession(input) {
    const removal = normalizeRemovalInput(input);
    return this._write([SESSION_STORE, REQUEST_STORE], async ({ sessions, requests }) => {
      const existing = await requestValue(sessions.get(removal.sessionId));
      if (!existing) {
        return mutationResult(this.mode, "missing");
      }
      if (existing.version !== removal.expectedVersion) {
        return mutationResult(this.mode, "version_conflict", {
          session: copyForReturn(existing),
        });
      }

      const requestKeys = await requestValue(
        requests.index("by_session").getAllKeys(removal.sessionId),
      );
      for (const key of requestKeys) {
        requests.delete(key);
      }
      sessions.delete(removal.sessionId);

      return mutationResult(this.mode, "removed", {
        removedRequests: requestKeys.length,
        sessionId: removal.sessionId,
      });
    });
  }

  async listSessions() {
    return this._read([SESSION_STORE], async ({ sessions }) => {
      const records = await requestValue(sessions.getAll());
      return {
        mode: this.mode,
        sessions: records
          .map(copyForReturn)
          .sort((left, right) => right.updatedAt.localeCompare(left.updatedAt)),
      };
    });
  }

  async getSession(sessionId) {
    const identifier = requireIdentifier(sessionId, "sessionId");
    return this._read([SESSION_STORE], async ({ sessions }) => ({
      mode: this.mode,
      session: nullableCopy(await requestValue(sessions.get(identifier))),
    }));
  }

  async listRequests(sessionId) {
    const identifier = requireIdentifier(sessionId, "sessionId");
    return this._read([REQUEST_STORE], async ({ requests }) => {
      const records = await requestValue(requests.index("by_session").getAll(identifier));
      return {
        mode: this.mode,
        requests: records
          .map(copyForReturn)
          .sort((left, right) =>
            left.createdAt.localeCompare(right.createdAt) || left.requestId.localeCompare(right.requestId),
          ),
      };
    });
  }

  async getRequest(sessionId, requestId) {
    const key = requestKey(
      requireIdentifier(sessionId, "sessionId"),
      requireIdentifier(requestId, "requestId"),
    );
    return this._read([REQUEST_STORE], async ({ requests }) => ({
      mode: this.mode,
      request: nullableCopy(await requestValue(requests.get(key))),
    }));
  }

  close() {
    if (!this._closed) {
      this._closed = true;
      this._database.close();
    }
  }

  async _read(storeNames, callback) {
    this._assertOpen();
    let transaction;
    try {
      transaction = this._database.transaction(storeNames, "readonly");
    } catch (error) {
      throw toHistoryError(error, "storage_read_failed");
    }
    return runTransaction(transaction, callback, "storage_read_failed");
  }

  async _write(storeNames, callback) {
    this._assertOpen();
    let transaction;
    try {
      transaction = this._database.transaction(storeNames, "readwrite");
    } catch (error) {
      throw toHistoryError(error, "storage_write_failed");
    }
    return runTransaction(transaction, callback, "storage_write_failed");
  }

  _assertOpen() {
    if (this._closed) {
      throw new DurableChatHistoryError(
        "storage_closed",
        "Local durable chat history is closed and must be reopened.",
      );
    }
  }
}

class VolatileDurableChatHistory {
  constructor(namespace, failure) {
    this.mode = "volatile";
    this.namespace = namespace;
    this.requiresAnnouncement = true;
    this.failure = failure;
    this._closed = false;
    this._sessions = new Map();
    this._requests = new Map();
  }

  async createSession(input) {
    this._assertOpen();
    const sessionInput = normalizeSessionInput(input);
    const existing = this._sessions.get(sessionInput.sessionId);
    if (existing) {
      return mutationResult(this.mode, "existing", { session: copyForReturn(existing) });
    }

    const session = createSessionRecord(sessionInput, now());
    this._sessions.set(session.sessionId, session);
    return mutationResult(this.mode, "created", { session: copyForReturn(session) });
  }

  async recordSubmission(input) {
    this._assertOpen();
    const submission = normalizeSubmissionInput(input);
    const key = memoryRequestKey(submission.sessionId, submission.requestId);
    const existingRequest = this._requests.get(key);
    if (existingRequest) {
      if (sameSubmission(existingRequest, submission)) {
        return mutationResult(this.mode, "existing", {
          request: copyForReturn(existingRequest),
        });
      }
      return mutationResult(this.mode, "submission_conflict", {
        request: copyForReturn(existingRequest),
      });
    }

    const timestamp = now();
    const existingSession = this._sessions.get(submission.sessionId);
    const session = existingSession
      ? touchSession(existingSession, timestamp)
      : createSessionRecord(submission, timestamp);
    const request = createRequestRecord(submission, timestamp);
    this._sessions.set(session.sessionId, session);
    this._requests.set(key, request);

    return mutationResult(this.mode, "recorded", {
      request: copyForReturn(request),
      session: copyForReturn(session),
    });
  }

  async applyProjection(input) {
    this._assertOpen();
    const update = normalizeProjectionInput(input);
    const key = memoryRequestKey(update.sessionId, update.requestId);
    const existingRequest = this._requests.get(key);
    if (!existingRequest) {
      return mutationResult(this.mode, "missing");
    }

    const outcome = applyProjectionUpdate(existingRequest, update, now());
    if (!outcome.record) {
      return mutationResult(this.mode, outcome.disposition, {
        request: copyForReturn(existingRequest),
      });
    }

    const existingSession = this._sessions.get(update.sessionId);
    if (!existingSession) {
      throw new DurableChatHistoryError(
        "corrupt_database",
        "A request record has no matching local session record.",
      );
    }

    const session = touchSession(existingSession, outcome.record.updatedAt);
    this._requests.set(key, outcome.record);
    this._sessions.set(session.sessionId, session);
    return mutationResult(this.mode, outcome.disposition, {
      request: copyForReturn(outcome.record),
      session: copyForReturn(session),
    });
  }

  async renameSession(input) {
    this._assertOpen();
    const rename = normalizeRenameInput(input);
    const existing = this._sessions.get(rename.sessionId);
    if (!existing) {
      return mutationResult(this.mode, "missing");
    }
    if (existing.version !== rename.expectedVersion) {
      return mutationResult(this.mode, "version_conflict", {
        session: copyForReturn(existing),
      });
    }

    const session = {
      ...existing,
      title: rename.title,
      updatedAt: now(),
      version: existing.version + 1,
    };
    this._sessions.set(session.sessionId, session);
    return mutationResult(this.mode, "renamed", { session: copyForReturn(session) });
  }

  async removeSession(input) {
    this._assertOpen();
    const removal = normalizeRemovalInput(input);
    const existing = this._sessions.get(removal.sessionId);
    if (!existing) {
      return mutationResult(this.mode, "missing");
    }
    if (existing.version !== removal.expectedVersion) {
      return mutationResult(this.mode, "version_conflict", {
        session: copyForReturn(existing),
      });
    }

    let removedRequests = 0;
    for (const [key, request] of this._requests) {
      if (request.sessionId === removal.sessionId) {
        this._requests.delete(key);
        removedRequests += 1;
      }
    }
    this._sessions.delete(removal.sessionId);
    return mutationResult(this.mode, "removed", {
      removedRequests,
      sessionId: removal.sessionId,
    });
  }

  async listSessions() {
    this._assertOpen();
    return {
      mode: this.mode,
      sessions: Array.from(this._sessions.values(), copyForReturn).sort(
        (left, right) => right.updatedAt.localeCompare(left.updatedAt),
      ),
    };
  }

  async getSession(sessionId) {
    this._assertOpen();
    const identifier = requireIdentifier(sessionId, "sessionId");
    return {
      mode: this.mode,
      session: nullableCopy(this._sessions.get(identifier)),
    };
  }

  async listRequests(sessionId) {
    this._assertOpen();
    const identifier = requireIdentifier(sessionId, "sessionId");
    return {
      mode: this.mode,
      requests: Array.from(this._requests.values())
        .filter((request) => request.sessionId === identifier)
        .map(copyForReturn)
        .sort((left, right) =>
          left.createdAt.localeCompare(right.createdAt) || left.requestId.localeCompare(right.requestId),
        ),
    };
  }

  async getRequest(sessionId, requestId) {
    this._assertOpen();
    const key = memoryRequestKey(
      requireIdentifier(sessionId, "sessionId"),
      requireIdentifier(requestId, "requestId"),
    );
    return {
      mode: this.mode,
      request: nullableCopy(this._requests.get(key)),
    };
  }

  close() {
    this._closed = true;
    this._sessions.clear();
    this._requests.clear();
  }

  _assertOpen() {
    if (this._closed) {
      throw new DurableChatHistoryError(
        "storage_closed",
        "Volatile durable chat history is closed and cannot be reused.",
      );
    }
  }
}

function normalizeSessionInput(input) {
  return {
    sessionId: requireIdentifier(input?.sessionId, "sessionId"),
    title: optionalText(input?.title, "title"),
  };
}

function normalizeSubmissionInput(input) {
  const normalizedSubmission = copyForStorage(input?.normalizedSubmission, "normalized submission", {
    required: true,
  });
  const hasTranscript = hasOwn(input, "transcript");
  const hasDraft = hasOwn(input, "draft");
  const hasProjection = hasOwn(input, "projection");
  const transcript = hasTranscript ? copyTranscript(input.transcript, "transcript") : [];
  const draft = hasDraft ? copyForStorage(input.draft, "draft", { required: true }) : null;
  const projection = hasProjection
    ? copyForStorage(input.projection, "projection", { required: true })
    : null;

  return {
    sessionId: requireIdentifier(input?.sessionId, "sessionId"),
    requestId: requireIdentifier(input?.requestId, "requestId"),
    title: optionalText(input?.title, "title"),
    idempotencyKey: requireIdentifier(input?.idempotencyKey, "idempotencyKey"),
    normalizedSubmission,
    transcript,
    draft,
    projection,
  };
}

function normalizeProjectionInput(input) {
  if (!hasOwn(input, "projection")) {
    throw new DurableChatHistoryError(
      "invalid_record",
      "Applied projections must include a projection value.",
    );
  }
  if (!hasOwn(input, "cursor")) {
    throw new DurableChatHistoryError(
      "invalid_record",
      "Applied projections must include a cursor.",
    );
  }

  const mode = input?.mode ?? "incremental";
  if (mode !== "incremental" && mode !== "snapshot") {
    throw new DurableChatHistoryError(
      "invalid_record",
      "Projection mode must be incremental or snapshot.",
    );
  }
  if (mode === "snapshot" && (!hasOwn(input, "transcript") || !hasOwn(input, "draft"))) {
    throw new DurableChatHistoryError(
      "invalid_record",
      "Snapshot replacement requires transcript, draft, projection, and cursor together.",
    );
  }
  if (input?.authoritativeReset === true && mode !== "snapshot") {
    throw new DurableChatHistoryError(
      "invalid_record",
      "An authoritative cursor reset requires a complete snapshot replacement.",
    );
  }
  if (input?.authoritativeReset === true && optionalVersion(input.expectedVersion) === undefined) {
    throw new DurableChatHistoryError(
      "invalid_record",
      "An authoritative cursor reset requires the current request version.",
    );
  }

  return {
    sessionId: requireIdentifier(input?.sessionId, "sessionId"),
    requestId: requireIdentifier(input?.requestId, "requestId"),
    cursor: normalizeCursor(input.cursor),
    projection: copyForStorage(input.projection, "projection", { required: true }),
    hasTranscript: hasOwn(input, "transcript"),
    transcript: hasOwn(input, "transcript")
      ? copyTranscript(input.transcript, "transcript")
      : undefined,
    hasDraft: hasOwn(input, "draft"),
    draft: hasOwn(input, "draft")
      ? copyForStorage(input.draft, "draft", { required: true })
      : undefined,
    terminal: input.terminal === true,
    expectedVersion: optionalVersion(input.expectedVersion),
    mode,
    authoritativeReset: input.authoritativeReset === true,
  };
}

function normalizeRenameInput(input) {
  return {
    sessionId: requireIdentifier(input?.sessionId, "sessionId"),
    title: requiredText(input?.title, "title"),
    expectedVersion: requiredVersion(input?.expectedVersion, "expectedVersion"),
  };
}

function normalizeRemovalInput(input) {
  if (input?.confirmed !== true) {
    throw new DurableChatHistoryError(
      "confirmation_required",
      "Removing local durable chat history requires explicit confirmation.",
    );
  }
  return {
    sessionId: requireIdentifier(input.sessionId, "sessionId"),
    expectedVersion: requiredVersion(input.expectedVersion, "expectedVersion"),
  };
}

function createSessionRecord(input, timestamp) {
  return {
    sessionId: input.sessionId,
    title: input.title,
    version: 1,
    createdAt: timestamp,
    updatedAt: timestamp,
  };
}

function touchSession(session, timestamp) {
  return {
    ...session,
    updatedAt: timestamp,
    version: session.version + 1,
  };
}

function createRequestRecord(input, timestamp) {
  return {
    sessionId: input.sessionId,
    requestId: input.requestId,
    idempotencyKey: input.idempotencyKey,
    normalizedSubmission: input.normalizedSubmission,
    transcript: input.transcript,
    draft: input.draft,
    projection: input.projection,
    cursor: null,
    terminal: false,
    version: 1,
    createdAt: timestamp,
    updatedAt: timestamp,
  };
}

function applyProjectionUpdate(existing, update, timestamp) {
  if (update.expectedVersion !== undefined && existing.version !== update.expectedVersion) {
    return { disposition: "version_conflict" };
  }
  if (existing.terminal && !update.terminal) {
    return { disposition: "terminal_preserved" };
  }

  const cursorComparison = compareCursors(update.cursor, existing.cursor);
  if (cursorComparison < 0 && !update.authoritativeReset) {
    return { disposition: "stale_cursor" };
  }

  const candidate = {
    ...existing,
    cursor: update.cursor,
    projection: update.projection,
    terminal: existing.terminal || update.terminal,
    updatedAt: timestamp,
    version: existing.version + 1,
  };
  if (update.hasTranscript) {
    candidate.transcript = update.transcript;
  }
  if (update.hasDraft) {
    candidate.draft = update.draft;
  }

  if (cursorComparison === 0) {
    if (sameAppliedState(existing, candidate)) {
      return { disposition: "already_applied" };
    }
    if (update.expectedVersion === undefined) {
      return { disposition: "cursor_conflict" };
    }
  }

  return { disposition: "applied", record: candidate };
}

function sameAppliedState(existing, candidate) {
  return (
    existing.terminal === candidate.terminal
    && deepEqual(existing.cursor, candidate.cursor)
    && deepEqual(existing.projection, candidate.projection)
    && deepEqual(existing.transcript, candidate.transcript)
    && deepEqual(existing.draft, candidate.draft)
  );
}

function compareCursors(left, right) {
  if (right === null || right === undefined) {
    return 1;
  }
  if (left.position !== right.position) {
    return left.position > right.position ? 1 : -1;
  }
  if (left.revision !== right.revision) {
    return left.revision > right.revision ? 1 : -1;
  }
  return 0;
}

function sameSubmission(existing, input) {
  return (
    existing.idempotencyKey === input.idempotencyKey
    && deepEqual(existing.normalizedSubmission, input.normalizedSubmission)
  );
}

function mutationResult(mode, disposition, values = {}) {
  return {
    mode,
    disposition,
    ...values,
  };
}

function databaseNameFor(namespace) {
  return `${DATABASE_PREFIX}${namespace}`;
}

function openDatabase(name) {
  if (typeof indexedDB === "undefined") {
    return Promise.reject(
      new DurableChatHistoryError(
        "storage_unavailable",
        "IndexedDB is unavailable in this browser context.",
      ),
    );
  }

  return new Promise((resolve, reject) => {
    let settled = false;
    let upgradeFailure;
    let request;
    try {
      request = indexedDB.open(name, DATABASE_VERSION);
    } catch (error) {
      reject(toHistoryError(error, "storage_open_failed"));
      return;
    }

    const rejectOnce = (error) => {
      if (!settled) {
        settled = true;
        reject(toHistoryError(error, "storage_open_failed"));
      }
    };

    request.onblocked = () => {
      rejectOnce(
        new DurableChatHistoryError(
          "storage_blocked",
          "Local durable chat history is blocked by another open database version.",
        ),
      );
    };
    request.onerror = () => {
      rejectOnce(upgradeFailure ?? request.error);
    };
    request.onupgradeneeded = (event) => {
      try {
        configureDatabase(request.result, request.transaction, event.oldVersion);
      } catch (error) {
        upgradeFailure = error;
        request.transaction.abort();
      }
    };
    request.onsuccess = () => {
      const database = request.result;
      if (settled) {
        database.close();
        return;
      }

      try {
        assertDatabaseSchema(database);
      } catch (error) {
        database.close();
        rejectOnce(error);
        return;
      }

      settled = true;
      resolve(database);
    };
  });
}

function configureDatabase(database, transaction, oldVersion) {
  if (oldVersion > DATABASE_VERSION) {
    throw new DurableChatHistoryError(
      "unsupported_schema",
      "The local durable chat history uses a newer unsupported schema.",
    );
  }
  if (oldVersion === 0) {
    const sessions = database.createObjectStore(SESSION_STORE, { keyPath: "sessionId" });
    sessions.createIndex("by_updated", "updatedAt");
    const requests = database.createObjectStore(REQUEST_STORE, {
      keyPath: ["sessionId", "requestId"],
    });
    requests.createIndex("by_session", "sessionId");
    return;
  }

  if (!transaction) {
    throw new DurableChatHistoryError(
      "corrupt_database",
      "Local durable chat history could not start its schema transaction.",
    );
  }
  throw new DurableChatHistoryError(
    "unsupported_schema",
    "Local durable chat history needs an explicit non-destructive migration.",
  );
}

function assertDatabaseSchema(database) {
  if (
    !database.objectStoreNames.contains(SESSION_STORE)
    || !database.objectStoreNames.contains(REQUEST_STORE)
  ) {
    throw new DurableChatHistoryError(
      "corrupt_database",
      "Local durable chat history has an incomplete schema.",
    );
  }
  const transaction = database.transaction([REQUEST_STORE], "readonly");
  const requests = transaction.objectStore(REQUEST_STORE);
  if (!requests.indexNames.contains("by_session")) {
    throw new DurableChatHistoryError(
      "corrupt_database",
      "Local durable chat history is missing its request index.",
    );
  }
}

async function runTransaction(transaction, callback, fallbackCode) {
  const completion = transactionComplete(transaction);
  const stores = {};
  for (let index = 0; index < transaction.objectStoreNames.length; index += 1) {
    const storeName = transaction.objectStoreNames.item(index);
    if (storeName !== null) {
      stores[storeName] = transaction.objectStore(storeName);
    }
  }

  let result;
  try {
    result = await callback(stores);
  } catch (error) {
    try {
      transaction.abort();
    } catch {
      // The transaction may already have completed or aborted.
    }
    await completion.catch(() => undefined);
    throw toHistoryError(error, fallbackCode);
  }

  try {
    await completion;
    return result;
  } catch (error) {
    throw toHistoryError(error, fallbackCode);
  }
}

function requestValue(request) {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

function transactionComplete(transaction) {
  return new Promise((resolve, reject) => {
    transaction.oncomplete = () => resolve();
    transaction.onabort = () => reject(transaction.error);
    transaction.onerror = () => reject(transaction.error);
  });
}

function normalizeCursor(cursor) {
  if (!isPlainObject(cursor)) {
    throw new DurableChatHistoryError(
      "invalid_record",
      "A durable chat cursor must be an object with a local position.",
    );
  }
  const position = requiredVersion(cursor.position, "cursor.position");
  const revision = cursor.revision === undefined
    ? 0
    : requiredVersion(cursor.revision, "cursor.revision");
  const normalized = { position, revision };
  if (hasOwn(cursor, "value")) {
    normalized.value = copyForStorage(cursor.value, "cursor value", { required: true });
  }
  return normalized;
}

function copyTranscript(value, label) {
  if (!Array.isArray(value)) {
    throw new DurableChatHistoryError(
      "invalid_record",
      `${label} must be an array and was not stored.`,
    );
  }
  return copyForStorage(value, label, { required: true });
}

function copyForStorage(value, label, options = {}) {
  if (value === undefined) {
    if (options.required) {
      throw new DurableChatHistoryError(
        "invalid_record",
        `${label} is required and was not stored.`,
      );
    }
    return undefined;
  }

  assertPersistable(value, label);
  try {
    return structuredClone(value);
  } catch (error) {
    throw new DurableChatHistoryError(
      "invalid_record",
      `${label} cannot be safely copied into local durable chat history.`,
      { cause: error },
    );
  }
}

function copyForReturn(value) {
  return structuredClone(value);
}

function nullableCopy(value) {
  return value === undefined ? null : copyForReturn(value);
}

function assertPersistable(value, label, ancestors = new Set()) {
  if (value === null || typeof value === "string" || typeof value === "boolean") {
    return;
  }
  if (typeof value === "number") {
    if (Number.isFinite(value)) {
      return;
    }
    throw invalidPersistedValue(label);
  }
  if (Array.isArray(value)) {
    if (ancestors.has(value)) {
      throw invalidPersistedValue(label);
    }
    ancestors.add(value);
    for (const item of value) {
      assertPersistable(item, label, ancestors);
    }
    ancestors.delete(value);
    return;
  }
  if (!isPlainObject(value)) {
    throw invalidPersistedValue(label);
  }
  if (ancestors.has(value)) {
    throw invalidPersistedValue(label);
  }

  ancestors.add(value);
  for (const [key, item] of Object.entries(value)) {
    if (isCredentialLikeKey(key)) {
      ancestors.delete(value);
      throw new DurableChatHistoryError(
        "sensitive_field",
        `${label} contains credential-like metadata and was not stored.`,
      );
    }
    assertPersistable(item, label, ancestors);
  }
  ancestors.delete(value);
}

function invalidPersistedValue(label) {
  return new DurableChatHistoryError(
    "invalid_record",
    `${label} must contain JSON-compatible data and was not stored.`,
  );
}

function isCredentialLikeKey(key) {
  const compact = key.toLowerCase().replace(/[^a-z0-9]/g, "");
  return (
    SENSITIVE_FIELD_NAMES.has(compact)
    || /(?:authorization|cookie|credential|function(?:s)?key|apikey|accesskey|secret|password)/.test(
      compact,
    )
    || /(?:auth|access|id|refresh|bearer)token$/.test(compact)
    || /headers?$/.test(compact)
  );
}

function requireIdentifier(value, name) {
  if (typeof value !== "string" || value.length === 0 || value.length > MAX_IDENTIFIER_LENGTH) {
    throw new DurableChatHistoryError(
      "invalid_record",
      `${name} must be a non-empty string no longer than ${MAX_IDENTIFIER_LENGTH} characters.`,
    );
  }
  return value;
}

function optionalText(value, name) {
  if (value === undefined) {
    return "";
  }
  if (typeof value !== "string") {
    throw new DurableChatHistoryError("invalid_record", `${name} must be text.`);
  }
  return value;
}

function requiredText(value, name) {
  if (typeof value !== "string" || value.length === 0) {
    throw new DurableChatHistoryError(
      "invalid_record",
      `${name} must be non-empty text.`,
    );
  }
  return value;
}

function requiredVersion(value, name) {
  if (!Number.isSafeInteger(value) || value < 0) {
    throw new DurableChatHistoryError(
      "invalid_record",
      `${name} must be a non-negative integer.`,
    );
  }
  return value;
}

function optionalVersion(value) {
  return value === undefined ? undefined : requiredVersion(value, "expectedVersion");
}

function requestKey(sessionId, requestId) {
  return [sessionId, requestId];
}

function memoryRequestKey(sessionId, requestId) {
  return JSON.stringify([sessionId, requestId]);
}

function now() {
  return new Date().toISOString();
}

function hasOwn(value, key) {
  return value !== null
    && typeof value === "object"
    && Object.prototype.hasOwnProperty.call(value, key);
}

function isPlainObject(value) {
  if (value === null || typeof value !== "object") {
    return false;
  }
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function deepEqual(left, right) {
  if (Object.is(left, right)) {
    return true;
  }
  if (left === null || right === null || typeof left !== "object" || typeof right !== "object") {
    return false;
  }
  if (Array.isArray(left) || Array.isArray(right)) {
    return Array.isArray(left)
      && Array.isArray(right)
      && left.length === right.length
      && left.every((value, index) => deepEqual(value, right[index]));
  }

  const leftKeys = Object.keys(left).sort();
  const rightKeys = Object.keys(right).sort();
  return (
    leftKeys.length === rightKeys.length
    && leftKeys.every((key, index) => key === rightKeys[index] && deepEqual(left[key], right[key]))
  );
}

function publicFailure(error) {
  return {
    code: error.code,
    message: error.message,
  };
}

function toHistoryError(error, fallbackCode) {
  if (error instanceof DurableChatHistoryError) {
    return error;
  }

  const name = typeof error?.name === "string" ? error.name : "";
  if (name === "VersionError") {
    return new DurableChatHistoryError(
      "unsupported_schema",
      "The local durable chat history uses a newer unsupported schema.",
      { cause: error },
    );
  }
  if (name === "QuotaExceededError") {
    return new DurableChatHistoryError(
      "storage_quota",
      "Browser storage is full; local durable chat history was not saved.",
      { cause: error },
    );
  }
  if (name === "SecurityError" || name === "NotAllowedError") {
    return new DurableChatHistoryError(
      "storage_denied",
      "Browser storage access was denied; local durable chat history was not saved.",
      { cause: error },
    );
  }
  if (name === "InvalidStateError") {
    return new DurableChatHistoryError(
      "storage_unavailable",
      "Browser storage is unavailable; local durable chat history was not saved.",
      { cause: error },
    );
  }
  if (name === "NotFoundError" || name === "DataError") {
    return new DurableChatHistoryError(
      "corrupt_database",
      "Local durable chat history is incomplete or corrupt and was left unchanged.",
      { cause: error },
    );
  }
  return new DurableChatHistoryError(
    fallbackCode,
    "Local durable chat history could not be updated.",
    { cause: error },
  );
}
