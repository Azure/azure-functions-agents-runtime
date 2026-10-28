//! Bi-directional JSON-RPC 2.0 over stdio between a language worker and `fhs-runtime`.
//!
//! Supports concurrent reverse RPC requests (`tool.call`) and notifications (`agent.stream`)
//! from the Rust runtime back to the language worker while a worker `agent.run` request
//! is in-flight.

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};

use serde::{Deserialize, Serialize};
use serde_json::Value;
use thiserror::Error;
use tokio::io::{AsyncBufRead, AsyncBufReadExt, AsyncWrite, AsyncWriteExt};
use tokio::sync::{Mutex, mpsc, oneshot};

use crate::config::TriggerSpec;
use crate::providers::{InferenceMode, ProviderSummary};
use crate::session_fs::StorageMode;
use crate::skills::SkillDescriptor;

#[derive(Debug, Error)]
pub enum RpcError {
    #[error("I/O error on stdio JSON-RPC stream: {0}")]
    Io(#[from] std::io::Error),
    #[error("JSON serialization error: {0}")]
    Json(#[from] serde_json::Error),
    #[error("peer closed stdio stream before responding")]
    PeerClosed,
    #[error("remote RPC error ({code}): {message}")]
    Remote { code: i32, message: String },
}

/// JSON-RPC 2.0 wire frame (can be Request, Response, or Notification).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct JsonRpcFrame {
    pub jsonrpc: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub id: Option<Value>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub method: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub params: Option<Value>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub result: Option<Value>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub error: Option<JsonRpcErrorPayload>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct JsonRpcErrorPayload {
    pub code: i32,
    pub message: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub data: Option<Value>,
}

impl JsonRpcFrame {
    pub fn request(id: Value, method: impl Into<String>, params: Value) -> Self {
        Self {
            jsonrpc: "2.0".to_string(),
            id: Some(id),
            method: Some(method.into()),
            params: Some(params),
            result: None,
            error: None,
        }
    }

    pub fn notification(method: impl Into<String>, params: Value) -> Self {
        Self {
            jsonrpc: "2.0".to_string(),
            id: None,
            method: Some(method.into()),
            params: Some(params),
            result: None,
            error: None,
        }
    }

    pub fn response_ok(id: Value, result: Value) -> Self {
        Self {
            jsonrpc: "2.0".to_string(),
            id: Some(id),
            method: None,
            params: None,
            result: Some(result),
            error: None,
        }
    }

    pub fn response_err(id: Value, code: i32, message: impl Into<String>) -> Self {
        Self {
            jsonrpc: "2.0".to_string(),
            id: Some(id),
            method: None,
            params: None,
            result: None,
            error: Some(JsonRpcErrorPayload {
                code,
                message: message.into(),
                data: None,
            }),
        }
    }
}

/// Optional metadata when an in-app Azure Function trigger (HTTP, Queue, Blob, etc.)
/// is exposed as an agent tool and synthesized in-process inside the language worker.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TriggerToolMetadata {
    /// e.g. `"plain"`, `"httpTrigger"`, `"queueTrigger"`, `"blobTrigger"`
    pub binding_type: String,
    /// Name of the Azure Function in the worker.
    #[serde(default)]
    pub function_name: Option<String>,
    /// HTTP method when synthesizing an in-process HTTP trigger.
    #[serde(default)]
    pub http_method: Option<String>,
    /// Route template when synthesizing an in-process HTTP trigger.
    #[serde(default)]
    pub route: Option<String>,
}

/// Tool descriptor registered by the host language worker (`app.tool()` / `[AgentTool]`).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct WorkerToolDescriptor {
    pub name: String,
    pub description: String,
    /// JSON Schema for the tool's input parameters (`{"type": "object", ...}`).
    #[serde(default = "default_empty_object_schema")]
    pub parameters: Value,
    /// Optional metadata describing how the worker synthesizes the in-process call.
    #[serde(default)]
    pub trigger_metadata: Option<TriggerToolMetadata>,
}

fn default_empty_object_schema() -> Value {
    serde_json::json!({
        "type": "object",
        "properties": {}
    })
}

/// `initialize` request parameters from the language worker.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct InitializeParams {
    pub app_root: PathBuf,
    #[serde(default)]
    pub env: HashMap<String, String>,
    #[serde(default)]
    pub tools: Vec<WorkerToolDescriptor>,
    /// Explicitly force deterministic mock inference for tests.
    #[serde(default)]
    pub mock_llm: Option<bool>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AgentSummary {
    pub slug: String,
    pub name: String,
    pub description: String,
    pub model: String,
    pub trigger: TriggerSpec,
    pub enabled_skills: Vec<String>,
    pub enabled_tools: Vec<String>,
}

/// `initialize` response returned to the language worker.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct InitializeResult {
    pub runtime_version: String,
    pub agents: Vec<AgentSummary>,
    pub skills: Vec<SkillDescriptor>,
    pub provider: Option<ProviderSummary>,
    pub inference_mode: InferenceMode,
    pub storage_mode: StorageMode,
}

/// `tools.register` request parameters.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RegisterToolsParams {
    pub tools: Vec<WorkerToolDescriptor>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RegisterToolsResult {
    pub registered_count: usize,
}

/// `agent.run` request parameters from `HostedSkill.RunAsync` / `HostedSkill.StreamAsync`.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AgentRunParams {
    pub agent_name: String,
    pub prompt: String,
    #[serde(default)]
    pub session_id: Option<String>,
    #[serde(default)]
    pub stream: bool,
}

/// `agent.run` response returned to the language worker.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AgentRunResult {
    pub content: String,
    pub session_id: String,
    pub agent_name: String,
    pub model: String,
    pub storage_mode: StorageMode,
    pub resumed: bool,
    pub tool_calls_made: Vec<String>,
}

/// `session.inspect` request parameters for verifying persisted session state.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SessionInspectParams {
    pub agent_name: String,
    pub session_id: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SessionInspectResult {
    pub has_persisted_state: bool,
    pub workspace_files: Vec<String>,
    pub session_state_files: Vec<String>,
    pub transcript: Option<String>,
}

/// Reverse RPC `tool.call` parameters sent from `fhs-runtime` to the language worker.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ToolCallParams {
    pub tool_name: String,
    pub call_id: String,
    pub session_id: String,
    pub arguments: Value,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub trigger_metadata: Option<TriggerToolMetadata>,
}

/// Response from the language worker for a `tool.call` request.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ToolCallResult {
    pub content: String,
    #[serde(default)]
    pub is_error: bool,
}

/// Streaming notification `agent.stream` sent from `fhs-runtime` to the language worker.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AgentStreamEvent {
    pub session_id: String,
    pub agent_name: String,
    /// `"delta"`, `"tool_call"`, `"tool_result"`, `"done"`, or `"error"`
    pub kind: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub content: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tool_name: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tool_call_id: Option<String>,
}

/// Bi-directional JSON-RPC peer over newline-delimited JSON streams.
pub struct StdioRpcPeer {
    outbound_tx: mpsc::UnboundedSender<String>,
    pending_outbound: Mutex<HashMap<String, oneshot::Sender<JsonRpcFrame>>>,
    next_id: AtomicU64,
}

impl StdioRpcPeer {
    pub fn new(outbound_tx: mpsc::UnboundedSender<String>) -> Arc<Self> {
        Arc::new(Self {
            outbound_tx,
            pending_outbound: Mutex::new(HashMap::new()),
            next_id: AtomicU64::new(1),
        })
    }

    pub async fn send_frame(&self, frame: &JsonRpcFrame) -> Result<(), RpcError> {
        let mut line = serde_json::to_string(frame)?;
        line.push('\n');
        self.outbound_tx
            .send(line)
            .map_err(|_| RpcError::PeerClosed)
    }

    /// Send a JSON-RPC notification (`agent.stream`, etc.) to the language worker.
    pub async fn notify<P: Serialize>(&self, method: &str, params: &P) -> Result<(), RpcError> {
        let val = serde_json::to_value(params)?;
        let frame = JsonRpcFrame::notification(method, val);
        self.send_frame(&frame).await
    }

    /// Send a reverse JSON-RPC request (`tool.call`) to the language worker and await its response.
    pub async fn call_worker<P: Serialize, R: for<'de> Deserialize<'de>>(
        &self,
        method: &str,
        params: &P,
    ) -> Result<R, RpcError> {
        let seq = self.next_id.fetch_add(1, Ordering::Relaxed);
        let id_str = format!("rt-{seq}");
        let id_val = Value::String(id_str.clone());
        let params_val = serde_json::to_value(params)?;
        let frame = JsonRpcFrame::request(id_val, method, params_val);

        let (tx, rx) = oneshot::channel();
        {
            let mut pending = self.pending_outbound.lock().await;
            pending.insert(id_str.clone(), tx);
        }

        if let Err(err) = self.send_frame(&frame).await {
            let mut pending = self.pending_outbound.lock().await;
            pending.remove(&id_str);
            return Err(err);
        }

        let response = rx.await.map_err(|_| RpcError::PeerClosed)?;
        if let Some(err) = response.error {
            return Err(RpcError::Remote {
                code: err.code,
                message: err.message,
            });
        }
        let result_val = response.result.unwrap_or(Value::Null);
        Ok(serde_json::from_value(result_val)?)
    }

    /// Complete a pending reverse RPC call when the reader loop receives a response frame.
    pub async fn resolve_inbound_response(&self, frame: JsonRpcFrame) -> bool {
        let Some(id_key) = frame.id.as_ref().map(id_to_key) else {
            return false;
        };
        let sender = {
            let mut pending = self.pending_outbound.lock().await;
            pending.remove(&id_key)
        };
        if let Some(tx) = sender {
            let _ = tx.send(frame);
            true
        } else {
            false
        }
    }
}

pub fn id_to_key(id: &Value) -> String {
    match id {
        Value::String(s) => s.clone(),
        other => other.to_string(),
    }
}

/// Spawn the writer task that drains serialized JSON-RPC lines to `writer`.
pub fn spawn_writer_loop<W>(
    mut writer: W,
    mut rx: mpsc::UnboundedReceiver<String>,
) -> tokio::task::JoinHandle<Result<(), std::io::Error>>
where
    W: AsyncWrite + Unpin + Send + 'static,
{
    tokio::spawn(async move {
        while let Some(line) = rx.recv().await {
            writer.write_all(line.as_bytes()).await?;
            writer.flush().await?;
        }
        Ok(())
    })
}

/// Drive the bi-directional JSON-RPC loop over arbitrary `reader` and `writer` streams.
pub async fn serve_jsonrpc_stream<R, W, H, Fut>(
    reader: R,
    writer: W,
    peer_out: Option<tokio::sync::oneshot::Sender<Arc<StdioRpcPeer>>>,
    handler: H,
) -> Result<(), RpcError>
where
    R: AsyncBufRead + Unpin + Send + 'static,
    W: AsyncWrite + Unpin + Send + 'static,
    H: Fn(Arc<StdioRpcPeer>, String, Value) -> Fut + Send + Sync + 'static,
    Fut: std::future::Future<Output = Result<Value, (i32, String)>> + Send + 'static,
{
    let (tx, rx) = mpsc::unbounded_channel::<String>();
    let peer = StdioRpcPeer::new(tx);
    if let Some(notify_peer) = peer_out {
        let _ = notify_peer.send(peer.clone());
    }
    let _writer_handle = spawn_writer_loop(writer, rx);
    let handler = Arc::new(handler);

    let mut lines = reader.lines();
    while let Some(line) = lines.next_line().await? {
        let trimmed = line.trim();
        if trimmed.is_empty() {
            continue;
        }
        let frame: JsonRpcFrame = match serde_json::from_str(trimmed) {
            Ok(f) => f,
            Err(err) => {
                let err_frame =
                    JsonRpcFrame::response_err(Value::Null, -32700, format!("Parse error: {err}"));
                let _ = peer.send_frame(&err_frame).await;
                continue;
            }
        };

        // Distinguish inbound request vs response to a reverse request (`tool.call`).
        if let Some(method) = frame.method.clone() {
            let id = frame.id.clone().unwrap_or(Value::Null);
            let params = frame.params.unwrap_or_else(|| serde_json::json!({}));
            let is_shutdown = method == "shutdown";
            let peer_clone = peer.clone();
            let handler_clone = handler.clone();

            // Spawn request handling onto its own task so the reader loop continues
            // reading `tool.call` responses while `agent.run` is still awaiting!
            let join = tokio::spawn(async move {
                match handler_clone(peer_clone.clone(), method, params).await {
                    Ok(result) => {
                        let resp = JsonRpcFrame::response_ok(id, result);
                        let _ = peer_clone.send_frame(&resp).await;
                    }
                    Err((code, msg)) => {
                        let resp = JsonRpcFrame::response_err(id, code, msg);
                        let _ = peer_clone.send_frame(&resp).await;
                    }
                }
            });

            if is_shutdown {
                let _ = join.await;
                break;
            }
        } else {
            peer.resolve_inbound_response(frame).await;
        }
    }

    Ok(())
}
