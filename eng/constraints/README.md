# ACA fixture constraints

This directory pins the dependency closure used by the ACA qualification fixture at
`tests/live/apps/aca-qualification/`. The fixture is deployed with Azure
Functions Flex remote build, so Oryx resolves `requirements.txt` on the server.
Fully pinned constraints prevent those remote builds from drifting when PyPI
publishes new compatible releases.

`aca-fixture-requirements.txt` is a pip/Oryx-compatible export of `uv.lock`.
It includes the runtime's core dependencies plus the `aca_sandbox` and
`monitor` extras, and deliberately omits the local project because deployment
prepends the exact wheel built by the pipeline. Platform markers keep the same
file valid for Linux Python 3.13 and 3.14.

Regenerate from the repository root with:

```powershell
uv export --frozen --no-dev --extra aca_sandbox --extra monitor --no-emit-project --no-hashes --no-header --no-annotate --format requirements.txt --output-file .\eng\constraints\aca-fixture-requirements.txt
```

The qualification-pipeline unit tests verify the checked-in export's package
closure and exact versions against `uv.lock`, reject pip directives and source
references, and evaluate its markers for both supported Python minors.
