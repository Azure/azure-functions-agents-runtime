use fhs_runtime::engine::RuntimeEngine;
use fhs_runtime::protocol::serve_jsonrpc_stream;
use tokio::io::BufReader;

#[tokio::main]
async fn main() {
    // Log diagnostics to stderr so stdout remains a clean JSON-RPC 2.0 channel.
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("warn")),
        )
        .with_writer(std::io::stderr)
        .init();

    let engine = RuntimeEngine::new();
    let stdin = BufReader::new(tokio::io::stdin());
    let stdout = tokio::io::stdout();

    let res = serve_jsonrpc_stream(stdin, stdout, None, move |peer, method, params| {
        let engine = engine.clone();
        async move { engine.dispatch(peer, method, params).await }
    })
    .await;

    if let Err(err) = res {
        eprintln!("fhs-runtime exited with error: {err}");
        std::process::exit(1);
    }
}
