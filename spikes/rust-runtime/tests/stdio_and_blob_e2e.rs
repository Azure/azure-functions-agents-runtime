//! End-to-end integration tests for `fhs-runtime`:
//! 1. Bi-directional stdio JSON-RPC (`initialize`, `agent.run`, reverse `tool.call` for plain and
//!    in-app HTTP trigger tools, streaming `agent.stream` events, and multi-turn session resumption).
//! 2. Azure Blob Storage `SessionFsProvider` (`BlobSessionBackend`) against an in-process HTTP
//!    Azure Blob Storage emulator verifying `SharedKey` HMAC-SHA256 auth, `hdi_isfolder=true`
//!    directory markers, file read/write/append/readdir, and multi-turn `agent.run` persistence on Blob!

use std::collections::BTreeMap;
use std::fs;
use std::sync::Arc;

use fhs_runtime::engine::RuntimeEngine;
use fhs_runtime::protocol::{
    AgentRunParams, AgentRunResult, InitializeParams, InitializeResult, JsonRpcFrame,
    SessionInspectParams, SessionInspectResult, ToolCallParams, ToolCallResult,
    TriggerToolMetadata, WorkerToolDescriptor, serve_jsonrpc_stream,
};
use fhs_runtime::session_fs::StorageMode;
use serde_json::json;
use tempfile::TempDir;
use tokio::io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader};
use tokio::net::TcpListener;
use tokio::sync::Mutex;

#[tokio::test]
async fn bi_directional_stdio_rpc_with_in_app_tools_and_multi_turn_sessions() {
    let tmp = TempDir::new().unwrap();

    // 1. Author agents.config.yaml, .agent.md, and skills/
    fs::write(
        tmp.path().join("agents.config.yaml"),
        "model: $GLOBAL_MODEL\ntimeout: 90\n",
    )
    .unwrap();

    let agents_dir = tmp.path().join("agents");
    fs::create_dir_all(&agents_dir).unwrap();
    fs::write(
        agents_dir.join("order_assistant.agent.md"),
        r#"---
name: Order Assistant ($DEPLOY_ENV)
description: Helps customers look up orders and invoke in-app functions
skills:
  - order-policy
tools:
  - lookup_order
  - calculate_shipping_http
---
You are the Order Assistant for $COMPANY_NAME.
Follow the order-policy skill when answering.
"#,
    )
    .unwrap();

    let skill_dir = tmp.path().join("skills").join("order-policy");
    fs::create_dir_all(&skill_dir).unwrap();
    fs::write(
        skill_dir.join("SKILL.md"),
        "# Order Policy: Always verify order ID and shipping rate.",
    )
    .unwrap();

    // Another skill not enabled for order_assistant:
    let unused_skill = tmp.path().join("skills").join("internal-audit");
    fs::create_dir_all(&unused_skill).unwrap();
    fs::write(unused_skill.join("SKILL.md"), "# Internal Audit").unwrap();

    // 2. Connect worker <-> runtime over in-memory duplex stdio streams.
    let (worker_to_rt, rt_stdin) = tokio::io::duplex(64 * 1024);
    let (rt_stdout, rt_to_worker) = tokio::io::duplex(64 * 1024);

    let engine = RuntimeEngine::new();
    let rt_task = tokio::spawn(async move {
        serve_jsonrpc_stream(
            BufReader::new(rt_stdin),
            rt_stdout,
            None,
            move |peer, method, params| {
                let engine = engine.clone();
                async move { engine.dispatch(peer, method, params).await }
            },
        )
        .await
    });

    let mut worker_writer = worker_to_rt;
    let mut worker_lines = BufReader::new(rt_to_worker).lines();

    // Step A: Worker sends `initialize` with env vars and two tools:
    // - `lookup_order`: plain `[AgentTool]` function
    // - `calculate_shipping_http`: in-app `[HttpTrigger]` function exposed as a tool over stdio!
    let init_params = InitializeParams {
        app_root: tmp.path().to_path_buf(),
        env: [
            ("GLOBAL_MODEL".to_string(), "gpt-4.1".to_string()),
            ("DEPLOY_ENV".to_string(), "staging".to_string()),
            ("COMPANY_NAME".to_string(), "Contoso".to_string()),
            (
                "FOUNDRY_PROJECT_ENDPOINT".to_string(),
                "https://contoso-ai.services.ai.azure.com/api/projects/orders".to_string(),
            ),
        ]
        .into_iter()
        .collect(),
        tools: vec![
            WorkerToolDescriptor {
                name: "lookup_order".to_string(),
                description: "Look up an order by order_id".to_string(),
                parameters: json!({
                    "type": "object",
                    "properties": {
                        "order_id": { "type": "string" }
                    },
                    "required": ["order_id"]
                }),
                trigger_metadata: Some(TriggerToolMetadata {
                    binding_type: "plain".to_string(),
                    function_name: Some("LookupOrderTool".to_string()),
                    http_method: None,
                    route: None,
                }),
            },
            WorkerToolDescriptor {
                name: "calculate_shipping_http".to_string(),
                description: "In-app HTTP trigger function calculating shipping cost".to_string(),
                parameters: json!({
                    "type": "object",
                    "properties": {
                        "order_id": { "type": "string" },
                        "zip": { "type": "string" }
                    },
                    "required": ["order_id", "zip"]
                }),
                trigger_metadata: Some(TriggerToolMetadata {
                    binding_type: "httpTrigger".to_string(),
                    function_name: Some("CalculateShippingHttp".to_string()),
                    http_method: Some("POST".to_string()),
                    route: Some("/api/shipping/calculate".to_string()),
                }),
            },
        ],
        mock_llm: Some(true),
    };

    send_worker_request(&mut worker_writer, 1, "initialize", &init_params).await;
    let init_resp_line = worker_lines.next_line().await.unwrap().unwrap();
    let init_frame: JsonRpcFrame = serde_json::from_str(&init_resp_line).unwrap();
    let init_result: InitializeResult =
        serde_json::from_value(init_frame.result.unwrap()).unwrap();

    assert_eq!(init_result.storage_mode, StorageMode::Local);
    assert_eq!(init_result.agents.len(), 1);
    assert_eq!(init_result.agents[0].slug, "order_assistant");
    assert_eq!(init_result.agents[0].name, "Order Assistant (staging)");
    assert_eq!(init_result.agents[0].model, "gpt-4.1");
    assert_eq!(init_result.agents[0].enabled_skills, vec!["order-policy"]);
    assert_eq!(
        init_result.agents[0].enabled_tools,
        vec!["calculate_shipping_http", "lookup_order"]
    );

    // Step B: Worker sends `agent.run` (turn 1) with `stream: true`.
    // While `agent.run` is in-flight, the Rust runtime sends `tool.call` requests over stdio
    // back to the worker, and the worker executes both tools in-process and replies!
    let run1 = AgentRunParams {
        agent_name: "order_assistant".to_string(),
        prompt: "Please lookup_order and calculate_shipping_http for {\"order_id\": \"ORD-42\", \"zip\": \"98052\"}".to_string(),
        session_id: Some("session-abc".to_string()),
        stream: true,
    };
    send_worker_request(&mut worker_writer, 2, "agent.run", &run1).await;

    let mut stream_events = Vec::new();
    let mut handled_tool_calls = Vec::new();
    let run1_result: AgentRunResult = loop {
        let line = worker_lines.next_line().await.unwrap().unwrap();
        let frame: JsonRpcFrame = serde_json::from_str(&line).unwrap();
        if let Some(method) = frame.method.as_deref() {
            match method {
                "tool.call" => {
                    let call_id = frame.id.clone().unwrap();
                    let params: ToolCallParams =
                        serde_json::from_value(frame.params.unwrap()).unwrap();
                    handled_tool_calls.push((
                        params.tool_name.clone(),
                        params
                            .trigger_metadata
                            .as_ref()
                            .map(|m| m.binding_type.clone())
                            .unwrap_or_default(),
                    ));
                    let tool_output = match params.tool_name.as_str() {
                        "lookup_order" => json!({
                            "order_id": params.arguments["order_id"],
                            "status": "PAID",
                            "items": 3
                        })
                        .to_string(),
                        "calculate_shipping_http" => json!({
                            "synthesized_trigger": "httpTrigger",
                            "route": params.trigger_metadata.as_ref().and_then(|m| m.route.clone()),
                            "shipping_usd": 12.50
                        })
                        .to_string(),
                        other => panic!("Unexpected tool call: {other}"),
                    };
                    let resp = JsonRpcFrame::response_ok(
                        call_id,
                        serde_json::to_value(ToolCallResult {
                            content: tool_output,
                            is_error: false,
                        })
                        .unwrap(),
                    );
                    let mut out = serde_json::to_string(&resp).unwrap();
                    out.push('\n');
                    worker_writer.write_all(out.as_bytes()).await.unwrap();
                    worker_writer.flush().await.unwrap();
                }
                "agent.stream" => {
                    stream_events.push(frame.params.unwrap());
                }
                other => panic!("Unexpected notification/request from runtime: {other}"),
            }
        } else if frame.id == Some(json!(2)) {
            break serde_json::from_value(frame.result.unwrap()).unwrap();
        }
    };

    assert!(!run1_result.resumed);
    assert_eq!(run1_result.session_id, "session-abc");
    assert_eq!(
        handled_tool_calls,
        vec![
            (
                "calculate_shipping_http".to_string(),
                "httpTrigger".to_string()
            ),
            ("lookup_order".to_string(), "plain".to_string()),
        ]
    );
    assert!(run1_result.content.contains("turn=1"));
    assert!(run1_result.content.contains("order-policy:# Order Policy"));
    assert!(run1_result.content.contains("shipping_usd"));
    assert!(!stream_events.is_empty());

    // Step C: Turn 2 on the same `session_id` ("session-abc") -> verifies `resumed == true`
    // and `turn=2` from persisted `SessionFs` state!
    let run2 = AgentRunParams {
        agent_name: "order_assistant".to_string(),
        prompt: "What was the status of my order?".to_string(),
        session_id: Some("session-abc".to_string()),
        stream: false,
    };
    send_worker_request(&mut worker_writer, 3, "agent.run", &run2).await;
    let run2_line = worker_lines.next_line().await.unwrap().unwrap();
    let run2_frame: JsonRpcFrame = serde_json::from_str(&run2_line).unwrap();
    let run2_result: AgentRunResult =
        serde_json::from_value(run2_frame.result.unwrap()).unwrap();

    assert!(run2_result.resumed);
    assert!(run2_result.content.contains("turn=2"));

    // Step D: Inspect persisted session state via `session.inspect`.
    let inspect_params = SessionInspectParams {
        agent_name: "order_assistant".to_string(),
        session_id: "session-abc".to_string(),
    };
    send_worker_request(&mut worker_writer, 4, "session.inspect", &inspect_params).await;
    let inspect_line = worker_lines.next_line().await.unwrap().unwrap();
    let inspect_frame: JsonRpcFrame = serde_json::from_str(&inspect_line).unwrap();
    let inspect_res: SessionInspectResult =
        serde_json::from_value(inspect_frame.result.unwrap()).unwrap();
    assert!(inspect_res.has_persisted_state);
    assert!(inspect_res.session_state_files.contains(&"turns.jsonl".to_string()));
    assert_eq!(
        inspect_res
            .transcript
            .as_deref()
            .unwrap_or("")
            .lines()
            .count(),
        2
    );

    // Shutdown cleanly.
    send_worker_request(&mut worker_writer, 5, "shutdown", &json!({})).await;
    let _ = worker_lines.next_line().await.unwrap();
    rt_task.await.unwrap().unwrap();
}

#[tokio::test]
async fn azure_blob_storage_session_fs_persists_multi_turn_sessions() {
    // Start an in-process mock Azure Blob Storage HTTP server verifying SharedKey auth and blob ops.
    let (blob_endpoint, blob_store) = start_mock_azure_blob_server().await;

    let tmp = TempDir::new().unwrap();
    fs::write(
        tmp.path().join("agent.md"),
        "---\nname: Blob Agent\nmodel: gpt-4o\n---\nYou persist sessions on Azure Blob Storage.\n",
    )
    .unwrap();

    let (worker_to_rt, rt_stdin) = tokio::io::duplex(64 * 1024);
    let (rt_stdout, rt_to_worker) = tokio::io::duplex(64 * 1024);

    let engine = RuntimeEngine::new();
    let rt_task = tokio::spawn(async move {
        serve_jsonrpc_stream(
            BufReader::new(rt_stdin),
            rt_stdout,
            None,
            move |peer, method, params| {
                let engine = engine.clone();
                async move { engine.dispatch(peer, method, params).await }
            },
        )
        .await
    });

    let mut worker_writer = worker_to_rt;
    let mut worker_lines = BufReader::new(rt_to_worker).lines();

    let conn_str = format!(
        "DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==;BlobEndpoint={blob_endpoint}/devstoreaccount1;"
    );

    let init_params = InitializeParams {
        app_root: tmp.path().to_path_buf(),
        env: [
            ("OPENAI_API_KEY".to_string(), "sk-test-key".to_string()),
            ("AzureWebJobsStorage".to_string(), conn_str),
            (
                "AZURE_FUNCTIONS_AGENTS_SESSION_CONTAINER".to_string(),
                "agent-sessions".to_string(),
            ),
        ]
        .into_iter()
        .collect(),
        tools: vec![],
        mock_llm: Some(true),
    };

    send_worker_request(&mut worker_writer, 1, "initialize", &init_params).await;
    let init_frame: JsonRpcFrame =
        serde_json::from_str(&worker_lines.next_line().await.unwrap().unwrap()).unwrap();
    let init_res: InitializeResult = serde_json::from_value(init_frame.result.unwrap()).unwrap();
    assert_eq!(init_res.storage_mode, StorageMode::Blob);

    // Turn 1 on Azure Blob Storage:
    send_worker_request(
        &mut worker_writer,
        2,
        "agent.run",
        &AgentRunParams {
            agent_name: "main".to_string(),
            prompt: "First turn stored in Azure Blob".to_string(),
            session_id: Some("blob-sess-1".to_string()),
            stream: false,
        },
    )
    .await;
    let r1_frame: JsonRpcFrame =
        serde_json::from_str(&worker_lines.next_line().await.unwrap().unwrap()).unwrap();
    let r1: AgentRunResult = serde_json::from_value(r1_frame.result.unwrap()).unwrap();
    assert_eq!(r1.storage_mode, StorageMode::Blob);
    assert!(!r1.resumed);
    assert!(r1.content.contains("turn=1"));

    // Turn 2 on Azure Blob Storage with same session_id:
    send_worker_request(
        &mut worker_writer,
        3,
        "agent.run",
        &AgentRunParams {
            agent_name: "main".to_string(),
            prompt: "Second turn resumed from Azure Blob".to_string(),
            session_id: Some("blob-sess-1".to_string()),
            stream: false,
        },
    )
    .await;
    let r2_frame: JsonRpcFrame =
        serde_json::from_str(&worker_lines.next_line().await.unwrap().unwrap()).unwrap();
    let r2: AgentRunResult = serde_json::from_value(r2_frame.result.unwrap()).unwrap();
    assert_eq!(r2.storage_mode, StorageMode::Blob);
    assert!(r2.resumed);
    assert!(r2.content.contains("turn=2"));

    // Verify the actual blobs stored in the mock Azure Blob server!
    {
        let store = blob_store.lock().await;
        assert!(store.contains_key("agent-sessions/copilot-native/main/blob-sess-1/session-state/turns.jsonl"));
        let turns_bytes = &store["agent-sessions/copilot-native/main/blob-sess-1/session-state/turns.jsonl"].0;
        let turns_text = String::from_utf8_lossy(turns_bytes);
        assert_eq!(turns_text.lines().count(), 2);
    }

    send_worker_request(&mut worker_writer, 4, "shutdown", &json!({})).await;
    let _ = worker_lines.next_line().await.unwrap();
    rt_task.await.unwrap().unwrap();
}

async fn send_worker_request<W: AsyncWriteExt + Unpin, P: serde::Serialize>(
    writer: &mut W,
    id: u64,
    method: &str,
    params: &P,
) {
    let frame = JsonRpcFrame::request(json!(id), method, serde_json::to_value(params).unwrap());
    let mut line = serde_json::to_string(&frame).unwrap();
    line.push('\n');
    writer.write_all(line.as_bytes()).await.unwrap();
    writer.flush().await.unwrap();
}

type BlobMap = Arc<Mutex<BTreeMap<String, (Vec<u8>, bool)>>>;

/// Minimal in-process HTTP server speaking the Azure Blob Storage REST protocol
/// (`PUT ?restype=container`, `PUT /{blob}`, `HEAD /{blob}`, `GET /{blob}`, `DELETE /{blob}`,
/// and `GET ?restype=container&comp=list&prefix=...`) and asserting `SharedKey` auth headers.
async fn start_mock_azure_blob_server() -> (String, BlobMap) {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let store: BlobMap = Arc::new(Mutex::new(BTreeMap::new()));
    let store_clone = store.clone();

    tokio::spawn(async move {
        while let Ok((mut stream, _)) = listener.accept().await {
            let store = store_clone.clone();
            tokio::spawn(async move {
                let mut buf = vec![0u8; 65536];
                let Ok(n) = stream.read(&mut buf).await else {
                    return;
                };
                if n == 0 {
                    return;
                }
                let raw = String::from_utf8_lossy(&buf[..n]);
                let Some((head_part, _)) = raw.split_once("\r\n\r\n") else {
                    return;
                };
                let body_offset = head_part.len() + 4;
                let body = &buf[body_offset..n];

                let mut lines = head_part.lines();
                let req_line = lines.next().unwrap_or("");
                let parts: Vec<&str> = req_line.split_whitespace().collect();
                if parts.len() < 2 {
                    return;
                }
                let method = parts[0];
                let target = parts[1];

                let mut has_shared_key = false;
                let mut is_folder_meta = false;
                for hdr in lines {
                    let lower = hdr.to_ascii_lowercase();
                    if lower.starts_with("authorization: sharedkey devstoreaccount1:") {
                        has_shared_key = true;
                    }
                    if lower.starts_with("x-ms-meta-hdi_isfolder: true") {
                        is_folder_meta = true;
                    }
                }
                if !has_shared_key {
                    let _ = stream
                        .write_all(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                        .await;
                    return;
                }

                let (path_raw, query_raw) = target.split_once('?').unwrap_or((target, ""));
                // Strip `/devstoreaccount1/` prefix.
                let after_account = path_raw
                    .strip_prefix("/devstoreaccount1/")
                    .unwrap_or(path_raw.trim_start_matches('/'));
                let decoded_path = percent_decode(after_account);

                if query_raw == "restype=container" && method == "PUT" {
                    let _ = stream
                        .write_all(b"HTTP/1.1 201 Created\r\nContent-Length: 0\r\n\r\n")
                        .await;
                    return;
                }

                if query_raw.starts_with("restype=container&comp=list") && method == "GET" {
                    let container = decoded_path.trim_end_matches('/');
                    let prefix_param = query_raw
                        .split('&')
                        .find_map(|kv| kv.strip_prefix("prefix="))
                        .map(percent_decode)
                        .unwrap_or_default();
                    let full_prefix = format!("{container}/{prefix_param}");
                    let map = store.lock().await;
                    let mut xml = String::from(
                        "<?xml version=\"1.0\" encoding=\"utf-8\"?><EnumerationResults><Blobs>",
                    );
                    for (k, (bytes, _)) in map.iter() {
                        if k.starts_with(&full_prefix) {
                            let blob_name = k
                                .strip_prefix(&format!("{container}/"))
                                .unwrap_or(k);
                            xml.push_str(&format!(
                                "<Blob><Name>{blob_name}</Name><Properties><Content-Length>{}</Content-Length></Properties></Blob>",
                                bytes.len()
                            ));
                        }
                    }
                    xml.push_str("</Blobs></EnumerationResults>");
                    let resp = format!(
                        "HTTP/1.1 200 OK\r\nContent-Type: application/xml\r\nContent-Length: {}\r\n\r\n{}",
                        xml.len(),
                        xml
                    );
                    let _ = stream.write_all(resp.as_bytes()).await;
                    return;
                }

                match method {
                    "PUT" => {
                        let mut map = store.lock().await;
                        map.insert(decoded_path, (body.to_vec(), is_folder_meta));
                        let _ = stream
                            .write_all(b"HTTP/1.1 201 Created\r\nContent-Length: 0\r\n\r\n")
                            .await;
                    }
                    "HEAD" => {
                        let map = store.lock().await;
                        if let Some((bytes, is_folder)) = map.get(&decoded_path) {
                            let folder_hdr = if *is_folder {
                                "x-ms-meta-hdi_isfolder: true\r\n"
                            } else {
                                ""
                            };
                            let resp = format!(
                                "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nLast-Modified: 2026-10-24T00:00:00Z\r\n{folder_hdr}\r\n",
                                bytes.len()
                            );
                            let _ = stream.write_all(resp.as_bytes()).await;
                        } else {
                            let _ = stream
                                .write_all(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
                                .await;
                        }
                    }
                    "GET" => {
                        let map = store.lock().await;
                        if let Some((bytes, _)) = map.get(&decoded_path) {
                            let hdr = format!(
                                "HTTP/1.1 200 OK\r\nContent-Length: {}\r\n\r\n",
                                bytes.len()
                            );
                            let _ = stream.write_all(hdr.as_bytes()).await;
                            let _ = stream.write_all(bytes).await;
                        } else {
                            let _ = stream
                                .write_all(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
                                .await;
                        }
                    }
                    "DELETE" => {
                        let mut map = store.lock().await;
                        map.remove(&decoded_path);
                        let _ = stream
                            .write_all(b"HTTP/1.1 202 Accepted\r\nContent-Length: 0\r\n\r\n")
                            .await;
                    }
                    _ => {}
                }
            });
        }
    });

    (format!("http://{addr}"), store)
}

fn percent_decode(input: &str) -> String {
    let bytes = input.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%'
            && i + 2 < bytes.len()
            && let Ok(hex) = std::str::from_utf8(&bytes[i + 1..i + 3])
            && let Ok(val) = u8::from_str_radix(hex, 16)
        {
            out.push(val);
            i += 3;
            continue;
        }
        out.push(bytes[i]);
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}
