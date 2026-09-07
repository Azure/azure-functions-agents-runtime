# 03 · チャット画面は API のクライアント

[目次](index.md) / 前: [HTTP API](02-http.md) / 次: [MAF とツール](04-maf-tools.md)

**今日のゴール:** 画面と agent 実行を切り離して考えます。
ブラウザーが読むのは HTML であり、`.agent.md` ではありません。

## ステップ 1 · 小さな画面を書く

新規: `chat.html`

```html
<!doctype html>
<html lang="ja">
<meta charset="utf-8">
<title>Mini Agent</title>
<h1>Mini Agent</h1>
<form id="chat">
  <input id="prompt" required placeholder="メッセージ">
  <button id="send">送信</button>
</form>
<pre id="log"></pre>
<script>
const form = document.querySelector("#chat");
const input = document.querySelector("#prompt");
const button = document.querySelector("#send");
const log = document.querySelector("#log");
form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const prompt = input.value;
  button.disabled = true;
  log.textContent += `You: ${prompt}\n`;
  try {
    const response = await fetch("/agents/greeter/chat", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({prompt}),
    });
    if (!response.ok) {
      throw new Error(`${response.status}: ${await response.text()}`);
    }
    const data = await response.json();
    log.textContent += `Agent: ${data.response}\n\n`;
    input.value = "";
  } catch (error) {
    log.textContent += `Error: ${error.message}\n`;
  } finally {
    button.disabled = false;
  }
});
</script>
</html>
```

モデルの出力を `innerHTML` に代入しない点に注目してください。
`textContent` で、HTML ではなく文字列として表示します。

## ステップ 2 · 同じ Host から配信する

`function_app.py` の末尾に追記:

```python
page = (root / "chat.html").read_text(encoding="utf-8")


@app.function_name(name="chat_page")
@app.route(route="chat", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def chat_page(req: func.HttpRequest) -> func.HttpResponse:
    return func.HttpResponse(page, mimetype="text/html")
```

Host を再起動して開きます。

```powershell
Start-Process "http://localhost:7071/chat"
```

ブラウザーの開発者ツールの Network で、送信時の
`POST /agents/greeter/chat` と `{"prompt":"..."}` を観察します。
HTML をファイルとして直接開くのではなく、Host 経由で開いてください。
同じ origin の API を使うので、この構成では CORS 設定は不要です。

## ここで止まって確認する

画面に履歴が残っていても、サーバーに送っているのは今回の `prompt` だけです。
**表示上の会話履歴と、モデルが参照する会話履歴は別物**です。
後の MAF 版も、まず単発実行のまま進めます。

API を PowerShell で呼んでも画面で呼んでも、同じ `Runner.run()` に届きます。
UI を変更しても loader や runner を変更する必要はありません。

## 本体を読む

[registration/endpoints.py](https://github.com/Azure/azure-functions-agents-runtime/blob/1351d6e7/src/azure_functions_agents/registration/endpoints.py)
の `_register_chat_page()`、`_register_http_chat()`、
`_register_http_chat_stream()` の三つの責務を見比べます。
画面の実体は `src/azure_functions_agents/public/` にあります。

本体の streaming は `run_agent_stream()` が生成する SSE イベントを画面へ流します。
今回の教材は単一の JSON 応答です。SSE を加えるのは、この往復が説明できてからで十分です。

**確認:** Markdown、HTML、prompt は、いつ・どのプロセスが読むものですか。
