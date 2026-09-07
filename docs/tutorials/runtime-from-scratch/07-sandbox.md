# 07 · Sandbox は別の実行場所

[目次](index.md) / 前: [Skills](06-skills.md) / 次: [A2A](08-a2a.md)

**今回は観察版だけでも十分です。** Azure 接続は後半の任意ステップです。
ここで言う Sandbox は、ACA (Azure Container Apps) Dynamic Sessions のコード実行環境です。
Python の仮想環境や `asyncio` を Sandbox と呼んでいるわけではありません。

## ステップ 1 · コードを「実行せず、送信する側」を作る

新規: `sandbox_adapter.py`

```python
import json
from typing import Protocol
from uuid import uuid4

from agent_framework import FunctionTool


class Sandbox(Protocol):
    async def execute(self, code: str, session_id: str) -> str: ...


class RecordingSandbox:
    async def execute(self, code: str, session_id: str) -> str:
        return json.dumps({
            "executed": False,
            "session_id": session_id,
            "received_code": code,
            "note": "Recording only. No Python code was executed.",
        })


def make_sandbox_tool(sandbox: Sandbox) -> FunctionTool:
    session_id = uuid4().hex

    async def execute_python(code: str) -> str:
        return await sandbox.execute(code, session_id)

    return FunctionTool(
        name="execute_python",
        description="Send Python code to the configured sandbox adapter.",
        func=execute_python,
    )
```

新規: `inspect_sandbox.py`

```python
import asyncio

from sandbox_adapter import RecordingSandbox

print(asyncio.run(RecordingSandbox().execute("print(6 * 7)", "demo")))
```

```powershell
python .\inspect_sandbox.py
```

出力は **42 ではなく `executed: false` と受け取ったコード**です。
この観察用 adapter は、本物の隔離環境を模倣したふりをしません。

MAF から呼ぶ場合、`toolset.py` に import を追加し、
`make_tools()` の list に以下の要素を加えます。

```python
from sandbox_adapter import RecordingSandbox, make_sandbox_tool
```

```python
        make_sandbox_tool(RecordingSandbox()),
```

CLI で「execute_python に print(6 * 7) を送って、ツールの結果をそのまま見せて」と依頼します。
`make_tools()` は 04 で各実行の中から呼んでいるため、同じ実行内の呼び出しは同じ ID、
次の実行は新しい ID になります。

**LLM が生成したコードを `eval` / `exec` / subprocess でこの PC 上に実行しないでください。**

## ステップ 2 · 任意: 既存 ACA session pool に接続する

前提は「使用を許可された既存 session pool の management endpoint」と、
その pool を実行できる ID です。ID には適切なスコープの
**Azure ContainerApps Session Executor** ロールが必要です。
ここではリソースを作成しません。接続がなければステップ 1 で終了できます。

[ACA Dynamic Sessions の概要](https://learn.microsoft.com/azure/container-apps/sessions)
も参照してください。以下は本体と同じ **`2025-10-02-preview` の `/executions` API** を対象にします。
古い `/code/execute` API の request envelope と混ぜないでください。

`requirements.txt` に追記し、インストールします。

```text
azure-identity>=1.25.3,<2
httpx>=0.28.1,<1
```

```powershell
python -m pip install -r .\requirements.txt
az login
$env:ACA_POOL_ENDPOINT = "<既存 pool の management endpoint>"
```

新規: `aca_sandbox.py`

```python
import json
from urllib.parse import urlsplit

import httpx
from azure.identity.aio import DefaultAzureCredential


class AcaSandbox:
    def __init__(self, endpoint: str) -> None:
        url = urlsplit(endpoint)
        if (
            url.scheme != "https"
            or not (url.hostname or "").endswith(".dynamicsessions.io")
            or url.username is not None
            or url.password is not None
            or url.port not in (None, 443)
            or url.query
            or url.fragment
        ):
            raise ValueError("Expected an HTTPS ACA session pool endpoint")
        self.endpoint = endpoint.rstrip("/")

    async def execute(self, code: str, session_id: str) -> str:
        async with DefaultAzureCredential() as credential:
            token = await credential.get_token("https://dynamicsessions.io/.default")
            async with httpx.AsyncClient(
                timeout=30, follow_redirects=False
            ) as http:
                response = await http.post(
                    f"{self.endpoint}/executions",
                    params={
                        "api-version": "2025-10-02-preview",
                        "identifier": session_id,
                    },
                    headers={"Authorization": f"Bearer {token.token}"},
                    json={
                        "codeInputType": "Inline",
                        "executionType": "Synchronous",
                        "code": code,
                        "timeoutInSeconds": 20,
                    },
                )
                response.raise_for_status()
                data = response.json()
        if not isinstance(data, dict) or not isinstance(data.get("result"), dict):
            raise ValueError("Unexpected ACA execution response")
        return json.dumps(data["result"], ensure_ascii=False)
```

信頼できる endpoint 以外に認証トークンを送らず、redirect にも追従しません。
これは本体の endpoint 検証という責務を残した小さな adapter です。
トークンや認証ヘッダーをログに出さないでください。

新規: `try_aca.py`

```python
import asyncio
import os
from uuid import uuid4

from aca_sandbox import AcaSandbox


async def main() -> None:
    sandbox = AcaSandbox(os.environ["ACA_POOL_ENDPOINT"])
    session_id = uuid4().hex
    print(await sandbox.execute("value = 40", session_id))
    print(await sandbox.execute("print(value + 2)", session_id))


asyncio.run(main())
```

```powershell
python .\try_aca.py
```

同じ session が有効な間は、2 回目の `stdout` に `42` が出るのが期待値です。
`stderr` も確認してください。**HTTP 成功と Python コードの成功は別**です。
session は pool のライフサイクルに従って失効するため、永続ストレージではありません。

その後 MAF とつなぐなら `toolset.py` に `os` と `AcaSandbox` の import を追加し、
`make_sandbox_tool(RecordingSandbox())` を次に置換します。

```python
        make_sandbox_tool(AcaSandbox(os.environ["ACA_POOL_ENDPOINT"])),
```

## 境界を整理する

```text
モデル → function_call(execute_python, code)
       → MAF がローカルの adapter 関数を呼ぶ
       → adapter がトークンを取得し ACA へ HTTP POST
       → ACA の session 内で Python を実行
       → stdout / stderr / result を tool result として返す
       → モデルが結果を読んで回答
```

ローカルで動くのは **送信処理**、コードを動かすのは **ACA** です。
一方、04 の `add` は信頼済みのローカル関数でした。
Skill の reader / script 実行が自動的にこの adapter を通るわけではありません。

## 本体を読む

[system_tools/sandbox.py](https://github.com/Azure/azure-functions-agents-runtime/blob/1351d6e7/src/azure_functions_agents/system_tools/sandbox.py)
の `create_sandbox_tools()` → `_execute_code()` と、
[registration/_handlers.py](https://github.com/Azure/azure-functions-agents-runtime/blob/1351d6e7/src/azure_functions_agents/registration/_handlers.py)
の `build_sandbox_tools_for_session()` を読みます。

本体は session ごとの tool を作り、明示 ID がなければ新しい GUID を使います。
credential / HTTP client の共有、初回 setup、テレメトリー等もあります。
教材は setup、接続共有、履歴との ID 連携、ブラウザー操作を省略しています。
Sandbox があっても、ネットワーク権限・機密データ・コストの境界は別途必要です。

**確認:** 二つのユーザーで同じ ACA session ID を共有すると、何が混ざり得ますか。
