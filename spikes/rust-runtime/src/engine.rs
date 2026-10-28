//! Core runtime engine tying together config discovery, skills, providers,
//! Azure Blob/Local `SessionFsProvider`, and `github-copilot-sdk` execution.

use std::collections::BTreeMap;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use async_trait::async_trait;
use github_copilot_sdk::session_fs::{SessionFsConfig, SessionFsConventions};
use github_copilot_sdk::tool::{ToolHandler, try_tool_parameters};
use github_copilot_sdk::types::{
    DeferMode, MessageOptions, ResumeSessionConfig, SessionConfig, SessionId, SystemMessageConfig,
    Tool, ToolInvocation, ToolResult, ToolResultExpanded,
};
use github_copilot_sdk::{Client, ClientMode, ClientOptions};
use serde_json::Value;
use tokio::sync::{Mutex, RwLock};
use uuid::Uuid;

use crate::config::{ResolvedAgent, discover_agents};
use crate::env::EnvMap;
use crate::protocol::{
    AgentRunParams, AgentRunResult, AgentStreamEvent, AgentSummary, InitializeParams,
    InitializeResult, RegisterToolsParams, RegisterToolsResult, SessionInspectParams,
    SessionInspectResult, StdioRpcPeer, ToolCallParams, ToolCallResult, WorkerToolDescriptor,
};
use crate::providers::{InferenceMode, ResolvedProvider, resolve_inference};
use crate::session_fs::{CopilotSessionFs, SESSION_STATE_ROOT, StorageRoute, WORKSPACE_ROOT};
use crate::skills::{SkillDescriptor, discover_skills, filter_skills_for_agent};

/// Tool handler that forwards `github-copilot-sdk` tool invocations over stdio
/// back to the host language worker (`tool.call`) and returns the worker's response.
pub struct StdioCallbackToolHandler {
    peer: Arc<StdioRpcPeer>,
    descriptor: WorkerToolDescriptor,
    session_id: String,
    agent_name: String,
    stream: bool,
    recorded_calls: Arc<Mutex<Vec<String>>>,
}

#[async_trait]
impl ToolHandler for StdioCallbackToolHandler {
    async fn call(&self, invocation: ToolInvocation) -> Result<ToolResult, github_copilot_sdk::Error> {
        {
            let mut calls = self.recorded_calls.lock().await;
            calls.push(self.descriptor.name.clone());
        }

        if self.stream {
            let _ = self
                .peer
                .notify(
                    "agent.stream",
                    &AgentStreamEvent {
                        session_id: self.session_id.clone(),
                        agent_name: self.agent_name.clone(),
                        kind: "tool_call".to_string(),
                        content: Some(invocation.arguments.to_string()),
                        tool_name: Some(self.descriptor.name.clone()),
                        tool_call_id: Some(invocation.tool_call_id.clone()),
                    },
                )
                .await;
        }

        let params = ToolCallParams {
            tool_name: self.descriptor.name.clone(),
            call_id: invocation.tool_call_id.clone(),
            session_id: self.session_id.clone(),
            arguments: invocation.arguments,
            trigger_metadata: self.descriptor.trigger_metadata.clone(),
        };

        let result: Result<ToolCallResult, _> = self.peer.call_worker("tool.call", &params).await;

        match result {
            Ok(res) => {
                if self.stream {
                    let _ = self
                        .peer
                        .notify(
                            "agent.stream",
                            &AgentStreamEvent {
                                session_id: self.session_id.clone(),
                                agent_name: self.agent_name.clone(),
                                kind: "tool_result".to_string(),
                                content: Some(res.content.clone()),
                                tool_name: Some(self.descriptor.name.clone()),
                                tool_call_id: Some(invocation.tool_call_id),
                            },
                        )
                        .await;
                }
                if res.is_error {
                    Ok(ToolResult::Expanded(ToolResultExpanded::new(
                        res.content,
                        "failure",
                    )))
                } else {
                    Ok(ToolResult::Text(res.content))
                }
            }
            Err(err) => Ok(ToolResult::Expanded(ToolResultExpanded::new(
                format!("{{\"error\":\"Worker tool call failed: {err}\"}}"),
                "failure",
            ))),
        }
    }
}

struct InitializedAppState {
    app_root: PathBuf,
    agents: BTreeMap<String, ResolvedAgent>,
    skills: Vec<SkillDescriptor>,
    tools: BTreeMap<String, WorkerToolDescriptor>,
    provider: Option<ResolvedProvider>,
    models: BTreeMap<String, String>,
    storage_route: StorageRoute,
    inference_mode: InferenceMode,
    copilot_client: Mutex<Option<Client>>,
}

/// Stateful runtime engine servicing a connected language worker over stdio.
#[derive(Default)]
pub struct RuntimeEngine {
    state: RwLock<Option<Arc<InitializedAppState>>>,
}

impl RuntimeEngine {
    pub fn new() -> Arc<Self> {
        Arc::new(Self {
            state: RwLock::new(None),
        })
    }

    pub async fn dispatch(
        &self,
        peer: Arc<StdioRpcPeer>,
        method: String,
        params: Value,
    ) -> Result<Value, (i32, String)> {
        match method.as_str() {
            "initialize" => {
                let req: InitializeParams =
                    serde_json::from_value(params).map_err(|e| (-32602, e.to_string()))?;
                let res = self.handle_initialize(req).await?;
                serde_json::to_value(res).map_err(|e| (-32603, e.to_string()))
            }
            "tools.register" => {
                let req: RegisterToolsParams =
                    serde_json::from_value(params).map_err(|e| (-32602, e.to_string()))?;
                let res = self.handle_register_tools(req).await?;
                serde_json::to_value(res).map_err(|e| (-32603, e.to_string()))
            }
            "agent.run" => {
                let req: AgentRunParams =
                    serde_json::from_value(params).map_err(|e| (-32602, e.to_string()))?;
                let res = self.handle_agent_run(peer, req).await?;
                serde_json::to_value(res).map_err(|e| (-32603, e.to_string()))
            }
            "session.inspect" => {
                let req: SessionInspectParams =
                    serde_json::from_value(params).map_err(|e| (-32602, e.to_string()))?;
                let res = self.handle_session_inspect(req).await?;
                serde_json::to_value(res).map_err(|e| (-32603, e.to_string()))
            }
            "shutdown" => Ok(serde_json::json!({ "ok": true })),
            other => Err((-32601, format!("Method not found: {other}"))),
        }
    }

    async fn handle_initialize(
        &self,
        params: InitializeParams,
    ) -> Result<InitializeResult, (i32, String)> {
        let env = EnvMap::from_process().with_overrides(params.env);
        let agents = discover_agents(&params.app_root, &env).map_err(|e| (-32001, e.to_string()))?;
        let skills = discover_skills(&params.app_root);
        let (inference_mode, provider) =
            resolve_inference(&env, params.mock_llm).map_err(|e| (-32002, e.to_string()))?;
        let storage_route = StorageRoute::from_env(&params.app_root, &env)
            .map_err(|e| (-32003, e.to_string()))?;

        let mut tools_map = BTreeMap::new();
        for tool in params.tools {
            tools_map.insert(tool.name.clone(), tool);
        }

        let mut agent_summaries = Vec::new();
        let mut models = BTreeMap::new();
        for agent in agents.values() {
            let model = if let Some(provider) = &provider {
                provider
                    .effective_model(agent.model.as_deref())
                    .map_err(|e| (-32002, e.to_string()))?
            } else {
                agent
                    .model
                    .as_deref()
                    .filter(|model| !model.trim().is_empty())
                    .or_else(|| env.get_trimmed("AZURE_FUNCTIONS_AGENTS_MODEL"))
                    .or_else(|| env.get_trimmed("FOUNDRY_MODEL"))
                    .unwrap_or("mock-model")
                    .to_string()
            };
            models.insert(agent.slug.clone(), model.clone());
            let skill_plan = filter_skills_for_agent(&skills, &agent.skills);
            let enabled_tools: Vec<String> = tools_map
                .keys()
                .filter(|name| agent.tools.is_enabled(name))
                .cloned()
                .collect();
            agent_summaries.push(AgentSummary {
                slug: agent.slug.clone(),
                name: agent.name.clone(),
                description: agent.description.clone(),
                model,
                trigger: agent.trigger.clone(),
                enabled_skills: skill_plan.enabled.into_iter().map(|s| s.name).collect(),
                enabled_tools,
            });
        }

        let result = InitializeResult {
            runtime_version: env!("CARGO_PKG_VERSION").to_string(),
            agents: agent_summaries,
            skills: skills.clone(),
            provider: provider.as_ref().map(ResolvedProvider::summary),
            inference_mode,
            storage_mode: storage_route.mode(),
        };

        let mut guard = self.state.write().await;
        *guard = Some(Arc::new(InitializedAppState {
            app_root: params.app_root,
            agents,
            skills,
            tools: tools_map,
            provider,
            models,
            storage_route,
            inference_mode,
            copilot_client: Mutex::new(None),
        }));

        Ok(result)
    }

    async fn handle_register_tools(
        &self,
        params: RegisterToolsParams,
    ) -> Result<RegisterToolsResult, (i32, String)> {
        let mut guard = self.state.write().await;
        let Some(existing) = guard.take() else {
            return Err((-32000, "Runtime is not initialized.".to_string()));
        };
        let mut tools = existing.tools.clone();
        for t in params.tools {
            tools.insert(t.name.clone(), t);
        }
        let count = tools.len();
        *guard = Some(Arc::new(InitializedAppState {
            app_root: existing.app_root.clone(),
            agents: existing.agents.clone(),
            skills: existing.skills.clone(),
            tools,
            provider: existing.provider.clone(),
            models: existing.models.clone(),
            storage_route: existing.storage_route.clone(),
            inference_mode: existing.inference_mode,
            copilot_client: Mutex::new(None),
        }));
        Ok(RegisterToolsResult {
            registered_count: count,
        })
    }

    async fn handle_agent_run(
        &self,
        peer: Arc<StdioRpcPeer>,
        params: AgentRunParams,
    ) -> Result<AgentRunResult, (i32, String)> {
        let app_state = {
            let guard = self.state.read().await;
            guard
                .clone()
                .ok_or_else(|| (-32000, "Runtime is not initialized.".to_string()))?
        };

        let agent = app_state
            .agents
            .get(&params.agent_name)
            .or_else(|| {
                app_state
                    .agents
                    .values()
                    .find(|a| a.name.eq_ignore_ascii_case(&params.agent_name))
            })
            .cloned()
            .ok_or_else(|| (-32004, format!("Unknown agent '{}'.", params.agent_name)))?;

        let model = app_state.models.get(&agent.slug).cloned().ok_or_else(|| {
            (
                -32002,
                format!("No resolved model for agent '{}'.", agent.slug),
            )
        })?;

        let session_id = params
            .session_id
            .filter(|s| !s.trim().is_empty())
            .unwrap_or_else(|| Uuid::new_v4().simple().to_string());

        let skill_plan = filter_skills_for_agent(&app_state.skills, &agent.skills);
        let workspace_dir = app_state.storage_route.local_dir.join("native").join("workspace");
        let workspace_str = workspace_dir.to_string_lossy().into_owned();

        let session_fs = CopilotSessionFs::open(
            &app_state.storage_route,
            &agent.slug,
            &session_id,
            &workspace_str,
            &skill_plan.enabled,
        )
        .await
        .map_err(|e| (-32005, format!("SessionFs open failed: {e}")))?;

        let resumed = session_fs
            .has_persisted_state()
            .await
            .map_err(|e| (-32005, format!("SessionFs state check failed: {e}")))?;

        let enabled_tools: Vec<WorkerToolDescriptor> = app_state
            .tools
            .values()
            .filter(|t| agent.tools.is_enabled(&t.name))
            .cloned()
            .collect();

        if app_state.inference_mode == InferenceMode::Mock {
            return self
                .run_mock_turn(
                    peer,
                    &app_state,
                    &agent,
                    &model,
                    &session_id,
                    resumed,
                    &params.prompt,
                    params.stream,
                    &skill_plan.enabled,
                    &enabled_tools,
                    session_fs,
                )
                .await;
        }

        self.run_copilot_sdk_turn(
            peer,
            &app_state,
            &agent,
            &model,
            &session_id,
            resumed,
            &params.prompt,
            params.stream,
            &skill_plan.enabled,
            &skill_plan.disabled_names,
            &enabled_tools,
            session_fs,
        )
        .await
    }

    #[allow(clippy::too_many_arguments)]
    async fn run_copilot_sdk_turn(
        &self,
        peer: Arc<StdioRpcPeer>,
        app_state: &Arc<InitializedAppState>,
        agent: &ResolvedAgent,
        model: &str,
        session_id: &str,
        resumed: bool,
        prompt: &str,
        stream: bool,
        enabled_skills: &[SkillDescriptor],
        disabled_skills: &[String],
        enabled_tools: &[WorkerToolDescriptor],
        session_fs: Arc<CopilotSessionFs>,
    ) -> Result<AgentRunResult, (i32, String)> {
        use github_copilot_sdk::session_fs::SessionFsProvider as _;

        let client = self.ensure_copilot_client(app_state).await?;
        let recorded_calls = Arc::new(Mutex::new(Vec::<String>::new()));

        let mut sdk_tools = Vec::with_capacity(enabled_tools.len());
        let mut available_tool_names = Vec::new();
        for desc in enabled_tools {
            let params_map = try_tool_parameters(desc.parameters.clone())
                .map_err(|e| (-32602, format!("Invalid schema for tool '{}': {e}", desc.name)))?;
            let handler = Arc::new(StdioCallbackToolHandler {
                peer: peer.clone(),
                descriptor: desc.clone(),
                session_id: session_id.to_string(),
                agent_name: agent.slug.clone(),
                stream,
                recorded_calls: recorded_calls.clone(),
            });
            let mut tool = Tool::new(desc.name.clone())
                .with_description(desc.description.clone())
                .with_handler(handler);
            tool.parameters = params_map;
            tool.skip_permission = true;
            tool.defer = Some(DeferMode::Never);
            available_tool_names.push(format!("custom:{}", desc.name));
            sdk_tools.push(tool);
        }

        if !enabled_skills.is_empty() {
            available_tool_names.extend([
                "builtin:skill".to_string(),
                "builtin:view".to_string(),
                "builtin:bash".to_string(),
            ]);
        }

        let copilot_session_id = SessionId::from(format!("{}-{session_id}", agent.slug));
        let provider = app_state.provider.as_ref().ok_or_else(|| {
            (
                -32002,
                "Foundry inference was selected without a resolved provider.".to_string(),
            )
        })?;
        let provider_cfg = provider.to_sdk_provider_config(model);
        let skill_dirs: Vec<PathBuf> = enabled_skills.iter().map(|s| s.path.clone()).collect();
        let sys_msg = SystemMessageConfig::new()
            .with_mode("replace")
            .with_content(agent.instructions.clone());

        let session = if !resumed {
            let mut cfg = SessionConfig::default();
            cfg.session_id = Some(copilot_session_id);
            cfg.model = Some(model.to_string());
            cfg.provider = Some(provider_cfg);
            cfg.system_message = Some(sys_msg);
            cfg.tools = Some(sdk_tools);
            cfg.available_tools = Some(available_tool_names);
            cfg.streaming = Some(stream);
            cfg.enable_config_discovery = Some(false);
            cfg.enable_skills = Some(!enabled_skills.is_empty());
            cfg.skill_directories = Some(skill_dirs);
            cfg.disabled_skills = Some(disabled_skills.to_vec());
            let fs_provider: Arc<dyn github_copilot_sdk::session_fs::SessionFsProvider> =
                session_fs.clone();
            cfg = cfg.with_session_fs_provider(fs_provider);
            client
                .create_session(cfg)
                .await
                .map_err(|e| (-32010, format!("Copilot create_session failed: {e}")))?
        } else {
            let mut cfg = ResumeSessionConfig::new(copilot_session_id);
            cfg.model = Some(model.to_string());
            cfg.provider = Some(provider_cfg);
            cfg.system_message = Some(sys_msg);
            cfg.tools = Some(sdk_tools);
            cfg.available_tools = Some(available_tool_names);
            cfg.streaming = Some(stream);
            cfg.enable_config_discovery = Some(false);
            cfg.enable_skills = Some(!enabled_skills.is_empty());
            cfg.skill_directories = Some(skill_dirs);
            cfg.disabled_skills = Some(disabled_skills.to_vec());
            let fs_provider: Arc<dyn github_copilot_sdk::session_fs::SessionFsProvider> =
                session_fs.clone();
            cfg = cfg.with_session_fs_provider(fs_provider);
            client
                .resume_session(cfg)
                .await
                .map_err(|e| (-32010, format!("Copilot resume_session failed: {e}")))?
        };

        // Forward streaming delta events if requested.
        let mut sub = session.subscribe();
        let peer_stream = peer.clone();
        let sid_owned = session_id.to_string();
        let slug_owned = agent.slug.clone();
        let stream_task = tokio::spawn(async move {
            if !stream {
                return;
            }
            while let Ok(ev) = sub.recv().await {
                if ev.event_type == "assistant.message_delta"
                    && let Some(delta_text) = ev.data.get("deltaContent").and_then(|v| v.as_str())
                {
                    let _ = peer_stream
                        .notify(
                            "agent.stream",
                            &AgentStreamEvent {
                                session_id: sid_owned.clone(),
                                agent_name: slug_owned.clone(),
                                kind: "delta".to_string(),
                                content: Some(delta_text.to_string()),
                                tool_name: None,
                                tool_call_id: None,
                            },
                        )
                        .await;
                }
            }
        });

        let mut msg_opts = MessageOptions::from(prompt);
        msg_opts.wait_timeout = Some(Duration::from_secs(agent.timeout_seconds));

        let send_res = session.send_and_wait(msg_opts).await;
        let _ = session.disconnect().await;
        stream_task.abort();

        let final_event = send_res
            .map_err(|e| (-32011, format!("Copilot send_and_wait failed: {e}")))?
            .ok_or_else(|| (-32011, "Copilot returned no final reply.".to_string()))?;

        let content = final_event
            .data
            .get("content")
            .and_then(|v| v.as_str())
            .map(ToString::to_string)
            .unwrap_or_else(|| final_event.data.to_string());

        let entry = serde_json::json!({
            "prompt": prompt,
            "reply": content,
        });
        let _ = session_fs
            .append_file(
                &format!("{SESSION_STATE_ROOT}/turns.jsonl"),
                &format!("{entry}\n"),
                None,
            )
            .await;

        if stream {
            let _ = peer
                .notify(
                    "agent.stream",
                    &AgentStreamEvent {
                        session_id: session_id.to_string(),
                        agent_name: agent.slug.clone(),
                        kind: "done".to_string(),
                        content: Some(content.clone()),
                        tool_name: None,
                        tool_call_id: None,
                    },
                )
                .await;
        }

        let tool_calls_made = recorded_calls.lock().await.clone();

        Ok(AgentRunResult {
            content,
            session_id: session_id.to_string(),
            agent_name: agent.slug.clone(),
            model: model.to_string(),
            storage_mode: app_state.storage_route.mode(),
            resumed,
            tool_calls_made,
        })
    }

    async fn ensure_copilot_client(
        &self,
        app_state: &Arc<InitializedAppState>,
    ) -> Result<Client, (i32, String)> {
        let mut lock = app_state.copilot_client.lock().await;
        if let Some(existing) = lock.as_ref() {
            return Ok(existing.clone());
        }
        let native_root = app_state.storage_route.local_dir.join("native");
        let workspace = native_root.join("workspace");
        tokio::fs::create_dir_all(&workspace)
            .await
            .map_err(|e| (-32005, format!("Failed to create workspace dir: {e}")))?;

        let mut opts = ClientOptions::default();
        opts.mode = ClientMode::Empty;
        opts.base_directory = Some(native_root.clone());
        opts.working_directory = native_root;
        opts.use_logged_in_user = Some(false);
        opts.session_fs = Some(SessionFsConfig::new(
            workspace.to_string_lossy().into_owned(),
            SESSION_STATE_ROOT,
            SessionFsConventions::Posix,
        ));

        let client = Client::start(opts)
            .await
            .map_err(|e| (-32009, format!("Failed to start Copilot CLI client: {e}")))?;
        *lock = Some(client.clone());
        Ok(client)
    }

    /// Deterministic scripted execution path for verifying `.agent.md` instructions,
    /// `SKILL.md` loading, bi-directional `tool.call` execution over stdio, and
    /// `CopilotSessionFs` multi-turn persistence (Local or Azure Blob) without live LLM calls.
    #[allow(clippy::too_many_arguments)]
    async fn run_mock_turn(
        &self,
        peer: Arc<StdioRpcPeer>,
        app_state: &Arc<InitializedAppState>,
        agent: &ResolvedAgent,
        model: &str,
        session_id: &str,
        resumed: bool,
        prompt: &str,
        stream: bool,
        enabled_skills: &[SkillDescriptor],
        enabled_tools: &[WorkerToolDescriptor],
        session_fs: Arc<CopilotSessionFs>,
    ) -> Result<AgentRunResult, (i32, String)> {
        use github_copilot_sdk::session_fs::SessionFsProvider as _;

        // 1. Read prior conversation turns from SessionFs (`/session-state/turns.jsonl`).
        let turns_path = format!("{SESSION_STATE_ROOT}/turns.jsonl");
        let prior_turns_raw = session_fs.read_file(&turns_path).await.unwrap_or_default();
        let prior_turn_count = prior_turns_raw.lines().filter(|l| !l.trim().is_empty()).count();

        // 2. Read enabled SKILL.md files through the SessionFs read-only skill policy.
        let mut loaded_skill_summaries = Vec::new();
        for skill in enabled_skills {
            let skill_md_path = skill.path.join("SKILL.md");
            if let Ok(text) = session_fs
                .read_file(&skill_md_path.to_string_lossy())
                .await
            {
                let first_line = text.lines().next().unwrap_or("").trim().to_string();
                loaded_skill_summaries.push(format!("{}:{}", skill.name, first_line));
            }
        }

        // 3. Invoke any registered worker tool mentioned in the prompt (or if prompt asks to run tools).
        let recorded_calls = Arc::new(Mutex::new(Vec::<String>::new()));
        let mut tool_outputs = Vec::new();
        for tool_desc in enabled_tools {
            if prompt.contains(&tool_desc.name) || prompt.to_ascii_lowercase().contains("use tool") {
                let handler = StdioCallbackToolHandler {
                    peer: peer.clone(),
                    descriptor: tool_desc.clone(),
                    session_id: session_id.to_string(),
                    agent_name: agent.slug.clone(),
                    stream,
                    recorded_calls: recorded_calls.clone(),
                };
                let args = extract_json_args_from_prompt(prompt)
                    .unwrap_or_else(|| serde_json::json!({ "prompt": prompt }));
                let inv: ToolInvocation = serde_json::from_value(serde_json::json!({
                    "sessionId": session_id,
                    "toolCallId": format!("call-{}", Uuid::new_v4().simple()),
                    "toolName": tool_desc.name,
                    "arguments": args
                }))
                .map_err(|e| (-32603, e.to_string()))?;
                if let Ok(tool_res) = handler.call(inv).await {
                    let text = match tool_res {
                        ToolResult::Text(t) => t,
                        ToolResult::Expanded(e) => e.text_result_for_llm,
                        _ => String::new(),
                    };
                    tool_outputs.push(format!("{}={}", tool_desc.name, text));
                }
            }
        }

        let content = format!(
            "[agent={slug} model={model} turn={turn} skills=[{skills}] tools=[{tools}] instructions_prefix={instr:?}] Reply to: {prompt}",
            slug = agent.slug,
            model = model,
            turn = prior_turn_count + 1,
            skills = loaded_skill_summaries.join(", "),
            tools = tool_outputs.join("; "),
            instr = agent.instructions.lines().next().unwrap_or(""),
            prompt = prompt,
        );

        if stream {
            for chunk in content.as_bytes().chunks(32) {
                let chunk_str = String::from_utf8_lossy(chunk).into_owned();
                let _ = peer
                    .notify(
                        "agent.stream",
                        &AgentStreamEvent {
                            session_id: session_id.to_string(),
                            agent_name: agent.slug.clone(),
                            kind: "delta".to_string(),
                            content: Some(chunk_str),
                            tool_name: None,
                            tool_call_id: None,
                        },
                    )
                    .await;
            }
        }

        let turn_record = serde_json::json!({
            "turn": prior_turn_count + 1,
            "prompt": prompt,
            "reply": content,
        });
        session_fs
            .append_file(&turns_path, &format!("{turn_record}\n"), None)
            .await
            .map_err(|e| (-32005, format!("Failed to persist turn to SessionFs: {e}")))?;

        if stream {
            let _ = peer
                .notify(
                    "agent.stream",
                    &AgentStreamEvent {
                        session_id: session_id.to_string(),
                        agent_name: agent.slug.clone(),
                        kind: "done".to_string(),
                        content: Some(content.clone()),
                        tool_name: None,
                        tool_call_id: None,
                    },
                )
                .await;
        }

        let tool_calls_made = recorded_calls.lock().await.clone();

        Ok(AgentRunResult {
            content,
            session_id: session_id.to_string(),
            agent_name: agent.slug.clone(),
            model: model.to_string(),
            storage_mode: app_state.storage_route.mode(),
            resumed,
            tool_calls_made,
        })
    }

    async fn handle_session_inspect(
        &self,
        params: SessionInspectParams,
    ) -> Result<SessionInspectResult, (i32, String)> {
        use github_copilot_sdk::session_fs::SessionFsProvider as _;

        let app_state = {
            let guard = self.state.read().await;
            guard
                .clone()
                .ok_or_else(|| (-32000, "Runtime is not initialized.".to_string()))?
        };
        let session_fs = CopilotSessionFs::open(
            &app_state.storage_route,
            &params.agent_name,
            &params.session_id,
            "",
            &[],
        )
        .await
        .map_err(|e| (-32005, e.to_string()))?;

        let has_persisted_state = session_fs
            .has_persisted_state()
            .await
            .map_err(|e| (-32005, e.to_string()))?;
        let workspace_files = session_fs
            .readdir(WORKSPACE_ROOT)
            .await
            .unwrap_or_default();
        let session_state_files = session_fs
            .readdir(SESSION_STATE_ROOT)
            .await
            .unwrap_or_default();
        let transcript = session_fs
            .read_file(&format!("{SESSION_STATE_ROOT}/turns.jsonl"))
            .await
            .ok();

        Ok(SessionInspectResult {
            has_persisted_state,
            workspace_files,
            session_state_files,
            transcript,
        })
    }
}

fn extract_json_args_from_prompt(prompt: &str) -> Option<Value> {
    let start = prompt.find('{')?;
    let end = prompt.rfind('}')?;
    if end <= start {
        return None;
    }
    serde_json::from_str(&prompt[start..=end]).ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn foundry_mode_starts_managed_sdk_runtime_without_model_contact() {
        let dir = tempfile::TempDir::new().unwrap();
        std::fs::write(
            dir.path().join("startup.agent.md"),
            "---\nname: Startup\nmodel: deployment\n---\nYou are a test agent.",
        )
        .unwrap();
        let engine = RuntimeEngine::new();
        let initialized = engine
            .handle_initialize(InitializeParams {
                app_root: dir.path().to_path_buf(),
                env: [
                    ("AZURE_FUNCTIONS_AGENTS_PROVIDER", "foundry"),
                    (
                        "FOUNDRY_PROJECT_ENDPOINT",
                        "https://startup.services.ai.azure.com/api/projects/test",
                    ),
                    ("FHS_MOCK_LLM", "false"),
                    ("AzureWebJobsStorage", ""),
                    (
                        "AZURE_FUNCTIONS_AGENTS_LOCAL_STORAGE_DIR",
                        dir.path().to_str().unwrap(),
                    ),
                ]
                .into_iter()
                .map(|(key, value)| (key.to_string(), value.to_string()))
                .collect(),
                tools: vec![],
                mock_llm: Some(false),
            })
            .await
            .unwrap();
        assert_eq!(initialized.inference_mode, InferenceMode::Foundry);
        assert_eq!(
            initialized.provider.unwrap().kind,
            crate::providers::ProviderKind::Foundry
        );
        let state = engine.state.read().await.clone().unwrap();
        let client = tokio::time::timeout(
            Duration::from_secs(60),
            engine.ensure_copilot_client(&state),
        )
        .await
        .expect("SDK runtime startup timed out")
        .expect("Foundry's SDK runtime must be available after cargo build");
        let ping = client.ping(Some("foundry-startup")).await;
        client.stop().await.unwrap();
        assert!(ping.is_ok(), "SDK runtime ping failed: {ping:?}");
    }
}
