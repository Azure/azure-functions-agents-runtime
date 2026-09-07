# 06 · Skill は必要になってから読む手順書

[目次](index.md) / 前: [非 HTTP トリガー](05-triggers.md) / 次: [Sandbox](07-sandbox.md)

**今日のゴール:** Skill の「利用」を、Python 関数の実行と区別します。
04 を終えていれば MAF で動かせます。05 で Echo に戻した場合は、
`bootstrap.py` を `Runner(MafBackend())` に戻し、Timer は停止したままにします。

## ステップ 1 · 手順書を一つ作る

新規フォルダー:

```powershell
New-Item -ItemType Directory -Path skills\short-report\references
```

新規: `skills\short-report\SKILL.md`

```markdown
---
name: short-report
description: Write a short report with a conclusion and one reason.
---
When asked to write a report:
1. Read references/format.md for the required layout.
2. Write the conclusion and one reason using that layout.
3. Do not invent missing facts.
```

新規: `skills\short-report\references\format.md`

```markdown
# Layout
結論: one sentence
理由: one sentence
```

Skill 自体は何も実行しません。まず「手順書を読む」だけを Python 関数として体験します。

新規: `skill_reader.py`

```python
import logging
from pathlib import Path

ROOT = Path(__file__).parent / "skills" / "short-report"
logger = logging.getLogger("mini_runtime")


def load_report_guide() -> str:
    """Read the trusted local short-report skill."""
    logger.info("reading SKILL.md now")
    return (ROOT / "SKILL.md").read_text(encoding="utf-8")
```

```powershell
python -c "from skill_reader import load_report_guide; print(load_report_guide())"
```

その後 `toolset.py` に import を追加し、`make_tools()` の list に一つ追加します。

```python
from skill_reader import load_report_guide
```

```python
        FunctionTool(
            name="load_report_guide",
            description="Read the short-report writing instructions.",
            func=load_report_guide,
        ),
```

CLI で「load_report_guide を呼んで手順を見せて」と入力します。
読む対象を固定しており、モデルから任意のファイルパスを受け取りません。
この段階では reference を読む関数がないため、**レポート作成全体ではなく本文取得だけ**の実験です。

**ここで止めて、ツール呼び出しによって文章が context に入る流れを確認します。**

## ステップ 2 · MAF の SkillsProvider に任せる

`toolset.py` から先ほど追加した `load_report_guide` の import と `FunctionTool` を取り除きます。
`add` は残します。手動の reader と SDK の reader を同時に使わないためです。
`skill_reader.py` は観察用として残して構いません。

新規: `skill_provider.py`

```python
from pathlib import Path

from agent_framework import SkillsProvider


def build_skills() -> SkillsProvider:
    skill = Path(__file__).parent / "skills" / "short-report"
    return SkillsProvider.from_paths(
        [skill],
        script_extensions=(),
        disable_load_skill_approval=True,
        disable_read_skill_resource_approval=True,
    )
```

これは自分で作った信頼済みの手順書を読むための設定です。
script は検出対象から外し、任意のローカルコードを実行させません。
外部から取った Skill に無条件で読み取りの自動承認を設定しないでください。

`maf_backend.py` に import を追加:

```python
from skill_provider import build_skills
```

`Agent(...)` の一行を置換:

```python
                context_providers=[build_skills()],
```

CLI で「short-report skill を使って、単体テストを追加する利点をレポートにして」と入力します。
期待する観察: `結論:` / `理由:` の形の回答。
本当に Skill が読まれたか調べるときは、SDK の `SkillsProvider.before_run()`、
`load_skill` / `read_skill_resource` の実装にブレークポイントを置きます。
**書式が合っているだけでは、読み込みが起きた証明にはなりません。**

## 三つの段階

| 段階 | モデルに渡るもの | 担当 |
| --- | --- | --- |
| 発見・広告 | Skill の名前、説明 | discovery / `SkillsProvider` |
| 本文のロード | 選んだ `SKILL.md` | `load_skill` |
| 追加資料のロード | `references/format.md` 等 | `read_skill_resource` |

必要なタイミングで読むことで、すべての手順書を最初から prompt に詰め込みません。
instructions は常に基本方針、skill は必要時に追加する作業知識、tool は実際の操作です。

`run_skill_script` という仕組みもありますが、**Skill を読むこと、script を実行すること、
ACA Sandbox でコードを実行することは別の機能**です。
実行場所は provider / script runner の構成で決まります。Skill という名前だけで隔離が保証されません。
この教材では scripts を有効にせず、次章で remote execution を明示的に扱います。

参照時点の本体も `SkillsProvider.from_paths()` に `script_runner` を渡していません。
MAF 1.13.0 では、file-based script は runner がないと実行できません。
承認を不要にする設定だけでは実行器は追加されず、ACA に自動転送もされません。
コードで登録する `@skill.script` の in-process 実行とは区別してください。

## 本体を読む

[discovery/skills.py](https://github.com/Azure/azure-functions-agents-runtime/blob/1351d6e7/src/azure_functions_agents/discovery/skills.py)
の `discover_skills()`、
[registration/capabilities.py](https://github.com/Azure/azure-functions-agents-runtime/blob/1351d6e7/src/azure_functions_agents/registration/capabilities.py)
の `build_capabilities()`、
[runner.py](https://github.com/Azure/azure-functions-agents-runtime/blob/1351d6e7/src/azure_functions_agents/runner.py)
の `_build_role_agent()` をこの順に読みます。

本体は発見した Skill を agent ごとの許可設定で絞り、個々の Skill directory を
`SkillsProvider.from_paths()` に渡します。教材は一つを明示的に選んでいます。

**確認:** Skill に「計算しなさい」と書くことと、`add` 関数を利用可能にすることは何が違いますか。
