# 01 · Markdown と runner を分ける

[目次](index.md) / 次: [HTTP API](02-http.md)

**今日のゴール:** Markdown の本文とユーザー入力が、別の引数として実行部分に届くことを観察します。
まだ AI は呼びません。Echo の表示は「instructions に従った回答」ではありません。

## ステップ 1 · 入力ファイルと型を作る

新規: `agents\greeter.agent.md`

```markdown
---
name: Greeter
---
You are a friendly guide. Answer briefly in Japanese.
```

新規: `models.py`

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class AgentSpec:
    slug: str
    name: str
    instructions: str
```

`name` は表示名、`slug` は入口の識別子、`instructions` は本文です。
型自身はファイルを読みません。`frozen=True` で起動後の意図しない書き換えを防ぎます。

## ステップ 2 · Markdown を型に変換する

新規: `loader.py`

```python
import re
from pathlib import Path

import frontmatter

from models import AgentSpec


def load_agents(directory: Path) -> dict[str, AgentSpec]:
    catalog: dict[str, AgentSpec] = {}
    for path in sorted(directory.glob("*.agent.md")):
        slug = path.name.removesuffix(".agent.md")
        if not re.fullmatch(r"[a-z][a-z0-9_]*", slug):
            raise ValueError(f"Invalid agent filename: {path.name}")
        with path.open(encoding="utf-8") as stream:
            post = frontmatter.load(stream)
        name = post.metadata.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"Missing name: {path}")
        if not post.content.strip():
            raise ValueError(f"Missing instructions: {path}")
        if slug in catalog:
            raise ValueError(f"Duplicate slug: {slug}")
        catalog[slug] = AgentSpec(slug, name.strip(), post.content)
    if not catalog:
        raise ValueError(f"No agent files in {directory}")
    return catalog
```

実行:

```powershell
python -c "from pathlib import Path; from loader import load_agents; print(load_agents(Path('agents')))"
```

期待する観察: `greeter`、`Greeter`、本文が表示されます。
`name:` を一時的に削除すると、起動前にエラーになります。戻して先へ進みます。

ここでは loader 内で最小限の検証も行います。本体では Pydantic による型変換、
global config とのマージ、マージ後の検証を別モジュールに分けています。

## ステップ 3 · 実行の窓口を作る

新規: `runner.py`

```python
import asyncio
import logging
from typing import Protocol

from models import AgentSpec

logger = logging.getLogger("mini_runtime")


class Backend(Protocol):
    async def reply(self, spec: AgentSpec, prompt: str) -> str: ...


class EchoBackend:
    async def reply(self, spec: AgentSpec, prompt: str) -> str:
        return f"[{spec.slug}]\ninstructions={spec.instructions}\nprompt={prompt}"


class Runner:
    def __init__(self, backend: Backend) -> None:
        self.backend = backend

    async def run(self, spec: AgentSpec, prompt: str) -> str:
        if not prompt.strip():
            raise ValueError("Prompt must not be empty")
        logger.info("run: slug=%s", spec.slug)
        async with asyncio.timeout(60):
            return await self.backend.reply(spec, prompt)
```

`Runner` は `.agent.md` の場所も `HttpRequest` も知りません。
`Backend` は実行方式を置換するための小さな約束です。今は Echo、後で MAF に変えます。

新規: `bootstrap.py`

```python
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType

from loader import load_agents
from models import AgentSpec
from runner import EchoBackend, Runner


def build_runtime(root: Path) -> tuple[Mapping[str, AgentSpec], Runner]:
    catalog = MappingProxyType(load_agents(root / "agents"))
    return catalog, Runner(EchoBackend())
```

新規: `cli.py`

```python
import asyncio
import logging
from pathlib import Path

from bootstrap import build_runtime

logging.basicConfig(level=logging.INFO)
catalog, runner = build_runtime(Path(__file__).parent)
prompt = input("You: ")
print(asyncio.run(runner.run(catalog["greeter"], prompt)))
```

```powershell
python .\cli.py
```

`こんにちは` と入力します。本文と入力が別々に表示されたら終了です。

## 観察実験

`cli.py` の `prompt = input(...)` で待っている間に Markdown の本文を書き換えます。
その後入力を確定しても、今回は**変更前の本文**が使われます。
もう一度 `python .\cli.py` を起動すると変更後になります。

つまり、この教材は「呼び出されるたびに md を読む」のではなく、
「起動時に読んだ仕様を使って呼び出しを処理する」設計です。
後の `func start` ではファイル変更による worker 再起動が入る場合があるため、
この実験はまず CLI で行います。

## 本体を読む

[config/loader.py](https://github.com/Azure/azure-functions-agents-runtime/blob/1351d6e7/src/azure_functions_agents/config/loader.py)
の `_load_agent_spec()` と、
[app.py](https://github.com/Azure/azure-functions-agents-runtime/blob/1351d6e7/src/azure_functions_agents/app.py)
の `create_function_app()` の前半だけ読みます。

本体は `AgentSpec → compose() → ResolvedAgent → AgentCapabilities → AgentCatalog` と進みます。
**利用可能な全ツールの発見**と、**その agent が使ってよいツールの選択**も別です。
教材は global config・権限フィルターを省略し、`AgentSpec` をそのまま保持します。

**確認:** `Runner.run()` の中に `frontmatter.load()` が不要な理由を説明できれば次へ進めます。
