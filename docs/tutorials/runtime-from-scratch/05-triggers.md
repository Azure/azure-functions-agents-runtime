# 05 · Timer / Queue も同じ runner に渡す

[目次](index.md) / 前: [MAF とツール](04-maf-tools.md) / 次: [Skills](06-skills.md)

**今日のゴール:** 「md を起動するトリガー」という特別な機構はないことを確かめます。
Functions が Python handler を呼び、handler がイベントを文字列に変換して runner に渡します。

繰り返し課金を避けるため、この章では `bootstrap.py` を **`Runner(EchoBackend())` に戻します**。
`EchoBackend` の import も戻してください。モデル接続は不要です。

## ステップ 1 · Storage を準備する

Timer の schedule 管理と Queue のために、利用可能な Azurite を起動します。
すでに動いているエミュレーターがあればそれを使い、二重起動しないでください。
未導入なら [Azurite の公式手順](https://learn.microsoft.com/azure/storage/common/storage-use-azurite) を使います。

`local.settings.json` の `Values` に追記:

```json
"AzureWebJobsStorage": "UseDevelopmentStorage=true"
```

JSON の前の行との間にカンマを入れてください。
`host.json` は以下に置換します。HTTP の `routePrefix` を残す点に注意します。

```json
{
  "version": "2.0",
  "extensionBundle": {
    "id": "Microsoft.Azure.Functions.ExtensionBundle",
    "version": "[4.0.0, 5.0.0)"
  },
  "extensions": {
    "http": {
      "routePrefix": ""
    }
  }
}
```

## ステップ 2 · Timer adapter を書く

新規: `event_adapter.py`

```python
import json
import logging

import azure.functions as func

from models import AgentSpec
from runner import Runner

logger = logging.getLogger("mini_runtime")


def make_timer_handler(spec: AgentSpec, runner: Runner):
    async def tick(timer: func.TimerRequest) -> None:
        payload = {"past_due": timer.past_due}
        prompt = f"Triggered by timer:\n{json.dumps(payload)}"
        answer = await runner.run(spec, prompt)
        logger.info("timer answer: %s", answer)

    return tick
```

`function_app.py` に import と末尾の登録処理を追記:

```python
from event_adapter import make_timer_handler
```

```python
timer_handler = make_timer_handler(catalog["greeter"], runner)
timer_function = app.timer_trigger(
    arg_name="timer",
    schedule="*/30 * * * * *",
    run_on_startup=False,
    use_monitor=True,
)(timer_handler)
app.function_name(name="greeter_timer")(timer_function)
```

6 フィールドの NCRONTAB で、これは 30 秒ごとです。
`arg_name="timer"` と handler の引数名 `timer` が一致しています。

```powershell
func start --port 7071
```

30 秒程度待ち、`timer answer:` を観察します。HTTP リクエストを送っていないのに、
`greeter.agent.md` の本文を保持した同じ `Runner.run()` が呼ばれます。
観察後は `Ctrl+C` で停止します。

**今日はここまでで十分です。** 次は任意の別セッションです。

## ステップ 3 · Queue も追加する

`event_adapter.py` 末尾に追記:

```python
def make_queue_handler(spec: AgentSpec, runner: Runner):
    async def receive(message: func.QueueMessage) -> None:
        prompt = "Queue message:\n" + message.get_body().decode("utf-8")
        answer = await runner.run(spec, prompt)
        logger.info("queue answer: %s", answer)

    return receive
```

`function_app.py` に import と登録を追加:

```python
from event_adapter import make_queue_handler
```

```python
queue_handler = make_queue_handler(catalog["greeter"], runner)
queue_function = app.queue_trigger(
    arg_name="message",
    queue_name="agent-inbox",
    connection="AzureWebJobsStorage",
)(queue_handler)
app.function_name(name="greeter_queue")(queue_function)
```

`connection` は接続文字列そのものではなく **設定名**です。
試験用の送信クライアントを入れます。

```powershell
python -m pip install "azure-storage-queue==12.13.*"
```

新規: `send_queue.py`

```python
from azure.core.exceptions import ResourceExistsError
from azure.storage.queue import QueueClient, TextBase64EncodePolicy

with QueueClient.from_connection_string(
    "UseDevelopmentStorage=true",
    queue_name="agent-inbox",
    message_encode_policy=TextBase64EncodePolicy(),
) as queue:
    try:
        queue.create_queue()
    except ResourceExistsError:
        print("Using existing agent-inbox queue")
    queue.send_message("hello from queue")
```

Host を再起動し、別の仮想環境ターミナルで:

```powershell
python .\send_queue.py
```

Functions Queue binding の既定の base64 decoding に合わせて送信しています。
期待する観察: `queue answer:` に `hello from queue` が含まれます。
この章を終えたら Timer の登録ブロックをコメントアウトし、不要な定期実行を止めます。

## 本体ではどう一般化しているか

[registration/triggers.py](https://github.com/Azure/azure-functions-agents-runtime/blob/1351d6e7/src/azure_functions_agents/registration/triggers.py)
の `_register_builtin_agent()` は、`trigger.type` からデコレーターを選び、
`trigger.args` を渡します。教材の Timer / Queue の登録をデータ駆動にした形です。

[registration/_handlers.py](https://github.com/Azure/azure-functions-agents-runtime/blob/1351d6e7/src/azure_functions_agents/registration/_handlers.py)
の `make_agent_handler()` が共通 handler を作り、
[_trigger_serialization.py](https://github.com/Azure/azure-functions-agents-runtime/blob/1351d6e7/src/azure_functions_agents/registration/_trigger_serialization.py)
が binding 固有のオブジェクトを JSON-safe な入力に変えます。
HTTP と違い、非 HTTP の実行結果は通常 HTTP 応答にはならず、ログ等へ流れます。

教材は trigger 設定を Python に直接書きました。本体のように md から登録したければ、
次の小課題として `schedule` を型に追加し、loader → registration に渡します。
handler の中で md を読み直さないことがポイントです。

Queue は再配信され得ます。失敗を握りつぶさず Host に伝え、
メール送信等の副作用には重複実行対策が必要です。教材の Echo には副作用がありません。

**確認:** Queue の入力型を変更するとき、runner と serializer のどちらを変更しますか。
