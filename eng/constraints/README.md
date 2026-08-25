# ACA fixture constraints

These files pin the dependency closure used by the ACA qualification fixture at
`tests/live/apps/aca-qualification/`. The fixture is deployed with Azure
Functions Flex remote build, so Oryx resolves `requirements.txt` on the server.
Fully pinned constraints prevent those remote builds from drifting when PyPI
publishes new compatible releases.

- `aca-fixture-requirements.in` is the human-editable source. It references the
  local runtime project with the `aca_sandbox` and `monitor` extras
  (`.[aca_sandbox,monitor]`), so uv reads the runtime dependencies and both
  extras from `pyproject.toml`.
- `aca-fixture-py313.txt` and `aca-fixture-py314.txt` are pip/Oryx-consumable
  requirements locks for the Linux Flex remote-build target. They deliberately
  omit the local `azurefunctions-agents-runtime` package because the deployment
  package installs the wheel built by the pipeline.

Regenerate from the repository root with:

```powershell
uv pip compile .\eng\constraints\aca-fixture-requirements.in --python-version 3.13 --python-platform x86_64-manylinux2014 --format requirements.txt --no-emit-package azurefunctions-agents-runtime -o .\eng\constraints\aca-fixture-py313.txt
uv pip compile .\eng\constraints\aca-fixture-requirements.in --python-version 3.14 --python-platform x86_64-manylinux2014 --format requirements.txt --no-emit-package azurefunctions-agents-runtime -o .\eng\constraints\aca-fixture-py314.txt
```

These commands update the existing files in place, so uv uses the current pins
as preferences and keeps an extra-only change surgical. Use uv's explicit
upgrade options when the intent is to refresh unrelated dependency versions.

If the two lock files diverge, refresh the deployed fixture requirements from the
lock that matches the Function App runtime version before packaging.
