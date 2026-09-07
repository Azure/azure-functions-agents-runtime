# 10 · 「動的」を二つに分けて理解する

[目次](index.md) / 前: [DAG](09-dag.md) / 次: [Durable](11-durable.md)

**別々の回で進める二つの実験です。**
Dynamic Workflow には、少なくとも次の二つの動的な要素があります。

| 種類 | 何が動的か | 誰が担当するか |
| --- | --- | --- |
| 計画の生成 | 実行前に要求に応じた DAG を作る | planner agent / LLM |
| 実行中の展開 | 上流データの件数や条件で実行対象を決める | 決定的な workflow engine |

実行中に scheduler が毎回 LLM に「次は何をするか」を問い合わせる、という設計ではありません。

## ステップ 1 · LLM にプランを提出させる

04 の MAF と 09 の DAG が前提です。
Timer は止め、Sandbox は `toolset.py` から外すか Recording 版にしておきます。
この実験の workflow が呼べる処理は、09 の純粋な整数演算だけです。

新規: `workflow_tool.py`

```python
import json

from agent_framework import FunctionTool

from dag import parse_plan, run_dag, validate_nodes


async def start_workflow(plan_json: str) -> str:
    """Validate and execute a small integer-arithmetic DAG."""
    raw = json.loads(plan_json)
    nodes = parse_plan(raw)
    validate_nodes(nodes)
    print("accepted plan:", json.dumps(raw, ensure_ascii=False))
    results = await run_dag(nodes)
    return json.dumps({"results": results})


def make_workflow_tool() -> FunctionTool:
    return FunctionTool(
        name="start_workflow",
        description=(
            "Execute a JSON plan with 1..16 tasks. "
            "Root: {tasks: [...]}. Each task has id, type='tool', tool, args, "
            "and optional depends_on (task ids). "
            "Allowed tools: constant(value), multiply(value, factor), add(a, b). "
            "Args are integers or full '${id.result}' references. "
            "Every reference must point to a depends_on ancestor. "
            "No cycles or other node types."
        ),
        func=start_workflow,
    )
```

`toolset.py` に import と list の要素を追加:

```python
from workflow_tool import make_workflow_tool
```

```python
        make_workflow_tool(),
```

`bootstrap.py` は `Runner(MafBackend())` にし、CLI から:

```text
start_workflow を使ってください。
最初に constant で 3 を作り、その結果を 2 倍するタスクと 10 倍するタスクを
並列に置き、最後に二つの結果を add で足す DAG を作って実行してください。
```

期待する観察: `accepted plan` → `wave` → 結果 `36`。
node ID や説明文はモデルにより変わり得ます。
**最終回答だけでなく、受理された JSON と scheduler の出力を見ます。**

不正なプランは `parse_plan()` / `validate_nodes()` が拒否します。
LLM の JSON を Python コードに変換して `exec` するわけではありません。
今回は JSON 文字列を一つの tool 引数として受けますが、本体はより豊かな型付きプランを扱います。

```text
instructions + prompt + start_workflow の schema
  → LLM がプランを引数にして tool call
  → 型・DAG・参照・許可ツールを検証
  → scheduler が決定的に実行
  → tool result
  → LLM が結果を説明
```

**この回はここまでです。**

## ステップ 2 · 上流データからノードを展開する

こちらは LLM 不要です。データ依存の展開だけを小さく体験します。

新規: `expand_plan.py`

```python
import asyncio

from dag import Node, run_dag


async def fetch_items() -> list[int]:
    return [2, 3, 4]


def materialize(items: list[int]) -> list[Node]:
    if len(items) > 8 or any(type(item) is not int for item in items):
        raise ValueError("Expected at most 8 integer items")
    return [
        Node(f"item{index}", "multiply", {"value": item, "factor": 2})
        for index, item in enumerate(items)
    ]


async def main() -> None:
    items = await fetch_items()
    nodes = materialize(items)
    results = await run_dag(nodes) if nodes else {}
    ordered = [results[node.id] for node in nodes]
    print({"ordered_results": ordered, "sum": sum(ordered)})


asyncio.run(main())
```

```powershell
python .\expand_plan.py
```

期待値は `[4, 6, 8]` と `18`。`fetch_items()` の戻り値を 2 件にすると、ノードも 2 個になります。
空配列なら結果は空、9 件以上は実行前に拒否します。

これは **materialization の概念を切り出したもの**です。
09 の parser に本体の `for_each` 構文を実装したわけではありません。
本体では上流の Activity 結果を得た後、論理ノードの `for_each` を個々の実行 instance に展開します。
`when` は条件を評価して実行 / skip を決め、結果を入力順で集約します。
そのため「論理ノード数」と「展開された実行数」は異なります。

## 本体の control flow

| 段階 | 本体で読むところ | 観察する値 |
| --- | --- | --- |
| planner の準備 | `workflows/integration.py:build_workflow_agent_integration()` | management tools、instructions の補足 |
| 詳しい作り方の追加 | packaged `data-driven-workflows` Skill | 必要時に読むプラン作成ガイド |
| プランの提出 | `workflows/tools.py` の `start_workflow` | tasks、agent / session の実行 context |
| 検証・認可 | `workflows/schema.py:validate_plan()` | `WorkflowPlanPolicy` |
| 永続実行の開始 | `schedule_new_orchestration()` | instance ID とシリアライズした入力 |
| 実行中の展開 | `engine.py:_run_dynamic_workflow()` | `when`、`for_each`、展開された instance |
| 実際の処理 | tool / sub_agent Activity | 関数の結果、または leaf agent の回答 |
| 観察・管理 | status / list / cancel / terminate tools | workflow の状態 |

コード入口:
[integration.py](https://github.com/Azure/azure-functions-agents-runtime/blob/1351d6e7/src/azure_functions_agents/workflows/integration.py) /
[tools.py](https://github.com/Azure/azure-functions-agents-runtime/blob/1351d6e7/src/azure_functions_agents/workflows/tools.py) /
[engine.py](https://github.com/Azure/azure-functions-agents-runtime/blob/1351d6e7/src/azure_functions_agents/workflows/engine.py)。

本体では通常の `subagents:` によるチャット中の委譲と、
`workflows.subagents:` で許可された DAG の leaf agent は別の権限です。
後者は Activity 内で `run_leaf_agent_task()` を呼びます。
「他の agent を呼ぶ」という共通点だけで A2A や workflow と同一視しないでください。

**教材との重要な差:** 教材の `start_workflow` はその場で完了まで待ちます。
本体は Durable に開始を依頼して ID を返し、後から状態確認できます。
次章では LLM をもう一度外し、この永続実行だけに集中します。

**確認:** プラン生成の非決定性を許せる場所と、replay のため決定性が必要な場所はどこですか。
