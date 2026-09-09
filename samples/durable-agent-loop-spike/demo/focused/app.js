"use strict";

const state = {
  activeSessionHandle: null,
  currentRun: null,
  dtsUrl: null,
  dtsWindow: null,
  pollTimer: null,
  shownHumanHandle: null,
};

const elements = {
  chat: document.getElementById("chat"),
  composer: document.getElementById("composer"),
  prompt: document.getElementById("prompt"),
  scenario: document.getElementById("scenario"),
  send: document.getElementById("sendBtn"),
  newSession: document.getElementById("newSessionBtn"),
  dts: document.getElementById("dtsBtn"),
  copyRun: document.getElementById("copyRunBtn"),
  sessionsList: document.getElementById("sessionsList"),
  sessionsEmpty: document.getElementById("sessionsEmpty"),
  statusLine: document.getElementById("statusLine"),
  runAlias: document.getElementById("runAlias"),
  sessionAlias: document.getElementById("sessionAlias"),
  sandboxAlias: document.getElementById("sandboxAlias"),
  sandboxState: document.getElementById("sandboxState"),
  runStatus: document.getElementById("runStatus"),
  runPhase: document.getElementById("runPhase"),
  modelSteps: document.getElementById("modelSteps"),
  toolCalls: document.getElementById("toolCalls"),
  proofOrchestrator: document.getElementById("proofOrchestrator"),
  proofModel: document.getElementById("proofModel"),
  proofTool: document.getElementById("proofTool"),
  proofCheckpoint: document.getElementById("proofCheckpoint"),
  humanCard: document.getElementById("humanCard"),
  humanQuestion: document.getElementById("humanQuestion"),
  humanChoicesForm: document.getElementById("humanChoicesForm"),
  humanChoices: document.getElementById("humanChoices"),
  humanChoiceSubmit: document.getElementById("humanChoiceSubmit"),
  humanForm: document.getElementById("humanForm"),
  humanAnswer: document.getElementById("humanAnswer"),
};

function csrfToken() {
  const item = document.cookie.split("; ").find((part) => part.startsWith("focused_csrf="));
  return item ? decodeURIComponent(item.split("=", 2)[1]) : "";
}

async function api(path, options = {}) {
  const request = { ...options, headers: { ...(options.headers || {}) } };
  if (request.method === "POST") {
    request.headers["Content-Type"] = "application/json";
    request.headers["X-CSRF-Token"] = csrfToken();
  }
  const response = await fetch(path, request);
  const body = await response.json();
  if (!response.ok) {
    throw new Error(body.error || `HTTP ${response.status}`);
  }
  return { status: response.status, body };
}

function setStatus(message, error = false) {
  elements.statusLine.textContent = message;
  elements.statusLine.classList.toggle("error", error);
}

function addBubble(kind, text) {
  const welcome = elements.chat.querySelector(".welcome");
  if (welcome) welcome.remove();
  const wrap = document.createElement("div");
  wrap.className = `bubble-wrap ${kind}`;
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  bubble.textContent = text;
  wrap.appendChild(bubble);
  elements.chat.appendChild(wrap);
  elements.chat.scrollTop = elements.chat.scrollHeight;
}

function resetRunStrip() {
  elements.runAlias.textContent = "—";
  elements.sandboxAlias.textContent = "—";
  elements.sandboxState.textContent = "—";
  elements.runStatus.textContent = "Ready";
  elements.runPhase.textContent = "—";
  elements.modelSteps.textContent = "0";
  elements.toolCalls.textContent = "0";
  elements.copyRun.disabled = true;
  setProofStep(elements.proofOrchestrator, "Ready");
  setProofStep(elements.proofModel, "Not scheduled");
  setProofStep(elements.proofTool, "Not scheduled");
  setProofStep(elements.proofCheckpoint, "Waiting");
}

function setProofStep(element, text, state = "") {
  element.textContent = text;
  const card = element.closest(".proof-step");
  card.classList.remove("active", "done");
  if (state) card.classList.add(state);
}

function renderRun(run) {
  state.currentRun = { ...(state.currentRun || {}), ...run };
  elements.runAlias.textContent = state.currentRun.run_alias || "—";
  elements.sessionAlias.textContent = state.currentRun.session_alias || "NEW";
  elements.runStatus.textContent = state.currentRun.status || "Running";
  elements.runPhase.textContent = state.currentRun.phase || "—";
  elements.modelSteps.textContent = String(state.currentRun.model_steps || 0);
  elements.toolCalls.textContent = String(state.currentRun.tool_calls || 0);
  elements.copyRun.disabled = !state.currentRun.run_alias;
  const status = String(state.currentRun.status || "Running");
  const modelSteps = Number(state.currentRun.model_steps || 0);
  const toolCalls = Number(state.currentRun.tool_calls || 0);
  const finished = terminal(status);
  setProofStep(
    elements.proofOrchestrator,
    `${status} · ${state.currentRun.phase || "starting"}`,
    finished ? "done" : "active",
  );
  setProofStep(
    elements.proofModel,
    modelSteps ? `${modelSteps} step${modelSteps === 1 ? "" : "s"}` : "Scheduling",
    modelSteps ? "done" : "active",
  );
  setProofStep(
    elements.proofTool,
    toolCalls ? `${toolCalls} sandbox tool${toolCalls === 1 ? "" : "s"}` : "Waiting for tool call",
    toolCalls ? "done" : "",
  );
}

async function loadSessions() {
  const { body } = await api("/api/sessions");
  elements.sessionsList.replaceChildren();
  elements.sessionsEmpty.hidden = body.sessions.length > 0;
  for (const session of body.sessions) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "session-item";
    button.classList.toggle("active", session.handle === state.activeSessionHandle);
    button.textContent = session.alias;
    button.addEventListener("click", () => {
      state.activeSessionHandle = session.handle;
      elements.sessionAlias.textContent = session.alias;
      setStatus(`Resuming ${session.alias} on the next turn.`);
      loadSessions().catch(showError);
    });
    elements.sessionsList.appendChild(button);
  }
}

async function inspectSandbox(runHandle) {
  try {
    const { body } = await api(`/api/runs/${encodeURIComponent(runHandle)}/sandbox`);
    elements.sandboxAlias.textContent = body.sandbox_instance_alias || "—";
    const generation = Number.isInteger(body.generation) ? ` · gen ${body.generation}` : "";
    const checkpoint = body.workspace_checkpoint_present ? " · checkpoint" : "";
    elements.sandboxState.textContent = `${body.state || "—"}${generation}${checkpoint}`;
    setProofStep(
      elements.proofTool,
      `${body.state || "Sandbox ready"} · ${body.sandbox_instance_alias || "isolated"}`,
      "done",
    );
    setProofStep(
      elements.proofCheckpoint,
      body.workspace_checkpoint_present ? "Workspace saved" : "Pending export",
      body.workspace_checkpoint_present ? "done" : "active",
    );
  } catch (_error) {
    // A retained sandbox may not exist until the first local tool step.
  }
}

function terminal(status) {
  return ["completed", "failed", "cancelled", "canceled", "terminated"].includes(
    String(status || "").toLowerCase(),
  );
}

async function pollRun() {
  if (!state.currentRun) return;
  const handle = state.currentRun.run_handle;
  try {
    const { body } = await api(`/api/runs/${encodeURIComponent(handle)}`);
    renderRun(body);
    setStatus(`${body.run_alias} · ${body.status || "running"} · ${body.phase || "starting"}`);
    inspectSandbox(handle);
    if (body.human_input && body.human_input.handle !== state.shownHumanHandle) {
      await showHumanInput(handle, body.human_input.handle);
    }
    if (terminal(body.status)) {
      clearInterval(state.pollTimer);
      state.pollTimer = null;
      elements.send.disabled = false;
      if (String(body.status).toLowerCase() === "completed") await loadResult(handle);
      return;
    }
  } catch (error) {
    showError(error);
  }
}

async function loadResult(handle) {
  for (let attempt = 0; attempt < 10; attempt += 1) {
    const { status, body } = await api(
      `/api/runs/${encodeURIComponent(handle)}/result`,
    );
    if (status === 202) {
      await new Promise((resolve) => setTimeout(resolve, 750));
      continue;
    }
    if (body.response) addBubble("assistant", body.response);
    setStatus(`${body.run_alias} completed.`);
    return;
  }
  throw new Error("Durable result was not available after completion.");
}

async function showHumanInput(runHandle, humanHandle) {
  const { body } = await api(
    `/api/runs/${encodeURIComponent(runHandle)}/human/${encodeURIComponent(humanHandle)}`,
  );
  state.shownHumanHandle = humanHandle;
  elements.humanQuestion.textContent = body.question;
  elements.humanChoices.replaceChildren();
  elements.humanChoiceSubmit.disabled = true;
  for (const [index, choice] of body.choices.entries()) {
    const choiceId = `human-choice-${index}`;
    const label = document.createElement("label");
    label.className = "human-choice-card";
    label.htmlFor = choiceId;
    const input = document.createElement("input");
    input.id = choiceId;
    input.type = "radio";
    input.name = "human-choice";
    input.value = choice;
    input.addEventListener("change", () => {
      elements.humanChoiceSubmit.disabled = false;
    });
    const text = document.createElement("span");
    text.textContent = choice;
    label.append(input, text);
    elements.humanChoices.appendChild(label);
  }
  elements.humanChoicesForm.hidden = body.choices.length === 0;
  elements.humanChoicesForm.dataset.runHandle = runHandle;
  elements.humanChoicesForm.dataset.humanHandle = humanHandle;
  elements.humanForm.hidden = !body.allow_free_text;
  elements.humanForm.dataset.runHandle = runHandle;
  elements.humanForm.dataset.humanHandle = humanHandle;
  elements.humanCard.hidden = false;
}

async function answerHuman(runHandle, humanHandle, answer) {
  await api(
    `/api/runs/${encodeURIComponent(runHandle)}/human/${encodeURIComponent(humanHandle)}`,
    { method: "POST", body: JSON.stringify({ answer }) },
  );
  addBubble("you", answer);
  elements.humanCard.hidden = true;
  state.shownHumanHandle = null;
  setStatus("Human input accepted. Durable execution resumed.");
  await pollRun();
}

async function startRun(prompt) {
  const payload = { prompt, scenario: elements.scenario.value };
  if (state.activeSessionHandle) payload.session_handle = state.activeSessionHandle;
  const { body } = await api("/api/runs", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  state.activeSessionHandle = body.session_handle;
  renderRun(body);
  elements.dts.disabled = !state.dtsUrl;
  elements.send.disabled = true;
  await loadSessions();
  setStatus(`${body.run_alias} accepted. Polling durable status…`);
  clearInterval(state.pollTimer);
  await pollRun();
  if (!terminal(state.currentRun.status)) state.pollTimer = setInterval(pollRun, 1200);
}

function showError(error) {
  setStatus(error instanceof Error ? error.message : String(error), true);
  elements.send.disabled = false;
}

elements.humanChoicesForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const selected = elements.humanChoicesForm.querySelector(
    'input[name="human-choice"]:checked',
  );
  if (!selected) return;
  elements.humanChoiceSubmit.disabled = true;
  answerHuman(
    elements.humanChoicesForm.dataset.runHandle,
    elements.humanChoicesForm.dataset.humanHandle,
    selected.value,
  ).catch(showError);
});

elements.composer.addEventListener("submit", (event) => {
  event.preventDefault();
  const prompt = elements.prompt.value.trim();
  if (!prompt) return;
  addBubble("you", prompt);
  elements.prompt.value = "";
  startRun(prompt).catch(showError);
});

elements.humanForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const answer = elements.humanAnswer.value.trim();
  if (!answer) return;
  elements.humanAnswer.value = "";
  answerHuman(
    elements.humanForm.dataset.runHandle,
    elements.humanForm.dataset.humanHandle,
    answer,
  ).catch(showError);
});

elements.newSession.addEventListener("click", () => {
  state.activeSessionHandle = null;
  state.currentRun = null;
  state.shownHumanHandle = null;
  clearInterval(state.pollTimer);
  state.pollTimer = null;
  elements.chat.replaceChildren();
  elements.humanCard.hidden = true;
  elements.sessionAlias.textContent = "NEW";
  resetRunStrip();
  loadSessions().catch(showError);
  setStatus("Ready for a new retained session.");
});

elements.copyRun.addEventListener("click", () => {
  if (!state.currentRun?.run_alias) return;
  navigator.clipboard.writeText(state.currentRun.run_alias).then(
    () => setStatus(`Copied ${state.currentRun.run_alias}.`),
    showError,
  );
});

elements.dts.addEventListener("click", () => {
  if (!state.dtsUrl) return;
  if (!state.dtsWindow || state.dtsWindow.closed) {
    const dashboard = window.open("about:blank", "durableTaskSchedulerDashboard");
    if (dashboard) {
      dashboard.opener = null;
      dashboard.location.replace(state.dtsUrl);
      state.dtsWindow = dashboard;
    }
  } else {
    state.dtsWindow.focus();
  }
  if (state.currentRun?.run_alias) {
    navigator.clipboard.writeText(state.currentRun.run_alias).catch(() => {});
    setStatus(`Opened DTS. Copied ${state.currentRun.run_alias} for correlation.`);
  }
});

Promise.all([api("/api/config"), loadSessions()])
  .then(([config]) => {
    state.dtsUrl = config.body.dts_dashboard_url;
    elements.dts.disabled = !state.dtsUrl;
  })
  .catch(showError);
