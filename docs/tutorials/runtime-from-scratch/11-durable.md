# 11 · DAG を Durable の上で実行する

[目次](index.md) / 前: [動的プラン](10-dynamic-plans.md)

**最後も 3 回に分けます。** 環境準備、実行、replay の観察です。
ここは「09 の in-memory scheduler が本体の Durable engine にどう対応するか」を学ぶ橋渡しです。
MAF や A2A を同時に動かす必要はありません。

## ステップ 1 · 独立した Functions プロジェクトを作る

既存の `function_app.py` を壊さないように別フォルダーを作ります。
元の教材フォルダーで次を実行します。

```powershell
New-Item -ItemType Directory -Path "$HOME\durable-runtime-lab"
Copy-Item .\dag.py, .\workflow_handlers.py, .\plan.json "$HOME\durable-runtime-lab"
Set-Location "$HOME\durable-runtime-lab"
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
```

新規: `requirements.txt`

```text
azure-functions>=2.1.0,<3
azure-functions-durable==2.0.0b2
```

```powershell
python -m pip install -r .\requirements.txt
```

**API 世代に注意:** このリポジトリは native Durable Python SDK の `2.0.0b2` を使っています。
旧 SDK の「context 一引数」「`context.get_input()`」「`context.task_all()`」の例を混ぜません。
ここでは二引数の orchestrator と `durabletask.task.when_all()` を使います。

既存の Azurite と DTS (Durable Task Scheduler) エミュレーター、または利用可能な DTS 接続が必要です。
エミュレーターのセットアップは
[本体の workflow ドキュメント](../../workflows.md) と
[DTS の公式ドキュメント](https://learn.microsoft.com/azure/azure-functions/durable/durable-task-scheduler/durable-task-scheduler)
を参照してください。教材のために既存 hub の状態を消去しないでください。

新規: `host.json`

```json
{
  "version": "2.0",
  "extensionBundle": {
    "id": "Microsoft.Azure.Functions.ExtensionBundle",
    "version": "[4.32.0, 5.0.0)"
  },
  "extensions": {
    "http": {"routePrefix": ""},
    "durableTask": {
      "hubName": "%TASKHUB_NAME%",
      "storageProvider": {
        "type": "azureManaged",
        "connectionStringName": "DURABLE_TASK_SCHEDULER_CONNECTION_STRING"
      }
    }
  }
}
```

新規: `local.settings.json`。二つの `<...>` は、利用する接続・hub に置換します。
これらはローカル設定であり、リポジトリに commit しません。

```json
{
  "IsEncrypted": false,
  "Values": {
    "FUNCTIONS_WORKER_RUNTIME": "python",
    "AzureWebJobsStorage": "UseDevelopmentStorage=true",
    "DURABLE_TASK_SCHEDULER_CONNECTION_STRING": "<existing-DTS-connection-string>",
    "TASKHUB_NAME": "<existing-task-hub>"
  }
}
```

ローカル DTS 接続文字列の形は `Endpoint=http://localhost:<port>;Authentication=None` です。
実際の port / hub は手元の構成に合わせます。02 と同じ `.gitignore` も作ってください。
Azurite だけでは、この `azureManaged` の実行バックエンドの代わりにはなりません。

## ステップ 2 · scheduler と Activity を分ける

新規: `function_app.py`。まず engine を記述します。

```python
import json

import azure.durable_functions as df
import azure.functions as func
from durabletask.task import OrchestrationContext, when_all

from dag import parse_plan, ready_nodes, resolve_args, validate_nodes
from workflow_handlers import call_tool

app = df.DFApp(http_auth_level=func.AuthLevel.ANONYMOUS)


@app.function_name(name="run_tool")
@app.activity_trigger(input_name="payload")
def run_tool(payload: dict) -> int:
    return call_tool(payload["tool"], payload["args"])


@app.function_name(name="workflow")
@app.orchestration_trigger(context_name="context")
def workflow(context: OrchestrationContext, raw: dict):
    nodes = parse_plan(raw)
    validate_nodes(nodes)
    pending = {node.id: node for node in nodes}
    results: dict[str, int] = {}
    while pending:
        ready = ready_nodes(pending, results)
        if not ready:
            raise ValueError("No runnable tasks")
        context.set_custom_status({
            "completed": len(results),
            "running": [node.id for node in ready],
        })
        tasks = [
            context.call_activity(
                "run_tool",
                input={"tool": node.tool, "args": resolve_args(node, results)},
            )
            for node in ready
        ]
        outputs = yield when_all(tasks)
        for node, output in zip(ready, outputs, strict=True):
            results[node.id] = output
            del pending[node.id]
    context.set_custom_status({"completed": len(results), "running": []})
    return {"results": results}
```

09 と見比べてください。
`ready_nodes` / `resolve_args` は変わりませんが、
`asyncio.TaskGroup` が `context.call_activity()` / `yield when_all()` に変わりました。

orchestrator は `async def` ではなく **generator 関数**です。
`yield` するのは一般の coroutine ではなく Durable が管理する Task です。
モデル呼び出し・ネットワーク I/O・外部への書き込みは Activity 側に置きます。

## ステップ 3 · 開始と状態確認を別 API にする

`function_app.py` に追記:

```python
@app.function_name(name="start_plan")
@app.route(route="plans", methods=["POST"])
@app.durable_client_input(client_name="client")
async def start_plan(
    req: func.HttpRequest, client: df.DurableFunctionsClient
) -> func.HttpResponse:
    try:
        raw = req.get_json()
        validate_nodes(parse_plan(raw))
    except ValueError as exc:
        return func.HttpResponse(str(exc), status_code=400)
    instance_id = await client.schedule_new_orchestration("workflow", input=raw)
    return func.HttpResponse(
        json.dumps({"instance_id": instance_id}),
        status_code=202,
        mimetype="application/json",
    )


@app.function_name(name="plan_status")
@app.route(route="plans/{instance_id}", methods=["GET"])
@app.durable_client_input(client_name="client")
async def plan_status(
    req: func.HttpRequest, client: df.DurableFunctionsClient
) -> func.HttpResponse:
    status = await client.get_status(req.route_params["instance_id"])
    if status.instance_id is None:
        return func.HttpResponse("Not found", status_code=404)
    return func.HttpResponse(
        json.dumps(status.to_json()), mimetype="application/json"
    )
```

```powershell
func start --port 7073
```

別のターミナルで、同じフォルダーから:

```powershell
$run = Invoke-RestMethod -Method Post `
  -Uri "http://localhost:7073/plans" `
  -ContentType "application/json" `
  -Body (Get-Content .\plan.json -Raw)
$run
$state = Invoke-RestMethod "http://localhost:7073/plans/$($run.instance_id)"
$state | ConvertTo-Json -Depth 10
```

開始は **202 と instance ID**、完了後の状態は `Completed`、output の `d` は `36` です。
まだ実行中なら、最後の状態取得を少し待って繰り返します。
`202` は「計算が成功した」という意味ではありません。
`Failed` なら状態と Host のログを確認し、新しいジョブを繰り返し送る前に原因を解決します。

この API は認証も所有者確認もないローカル教材用です。本番には公開しません。

## 別の回 · replay を目で見る

`function_app.py` の import に `from datetime import timedelta` を追加し、
`workflow()` の `validate_nodes(nodes)` の直後へ一行追加します。

```python
    yield context.create_timer(timedelta(seconds=20))
```

Host を再起動して新しいプランを開始し、待機中に **Functions Host だけ**を `Ctrl+C` で止めます。
DTS / Azurite は止めず、同じ設定・同じコードで Host を再起動します。
保存した instance ID の状態を確認すると、続きが進んで完了します。
実行中の instance があるまま orchestrator の構造を変更する実験は避けてください。

Durable は Python のスタックを丸ごと保存するわけではありません。
記録したイベント履歴から orchestrator を replay し、
完了済みの Activity / Timer については記録済み結果を使って状態を再構成します。
**Durable Timer は replay-safe** です。orchestrator 内で `time.sleep()` は使いません。

Activity は障害等で複数回呼ばれる可能性があります。
Durable を使っても外部副作用の exactly-once は自動保証されません。
今回の整数演算は繰り返しても同じ結果ですが、送信や書き込みには idempotency が必要です。

## 本体で増えているもの

[app.py](https://github.com/Azure/azure-functions-agents-runtime/blob/1351d6e7/src/azure_functions_agents/app.py)
は workflow を使う agent が存在すると `DFApp` を選び、workflow runtime を一度だけ登録します。
[workflows/engine.py](https://github.com/Azure/azure-functions-agents-runtime/blob/1351d6e7/src/azure_functions_agents/workflows/engine.py)
は Blueprint に orchestrator と Activity を登録します。
本体は `when_any` を利用した cancel との競合制御などもあり、
教材の単純な `when_all` とは実行管理の範囲が異なります。

tool Activity は handler catalog の許可対象を呼び、
sub_agent Activity は `run_leaf_agent_task()` によって専門 agent を実行します。
現在の policy による Activity 側の再認可、セッションごとの instance 所有権、
`when` / `for_each`、status、cancel / terminate は教材では省略しました。

## 最後の対応づけ

| 学んだもの | A2A を本体へ実装するときの意味 |
| --- | --- |
| catalog は起動時、runner は呼び出し時 | A2A request のたびに Markdown を再解析しない |
| HTTP / Timer / A2A は adapter | protocol 固有の型やエラーを runner に持ち込まない |
| tool は操作、skill は知識の追加 | Card の能力説明と実行権限を分ける |
| Sandbox session はリモート実行状態 | A2A context と安易に同一視しない |
| DAG planner と engine は別 | LLM の判断を replay 対象に入れない |
| Durable instance と A2A Task は別 | 長時間 Task を設計するとき、対応関係と状態変換を明示する |

ここまで書ければ、本体全体を一度に読む必要はありません。
変更したい入口から、catalog → handler → runner → MAF / workflow の経路だけをたどれます。
