"""Deployed ACA qualification fixture app.

Adds one fixture-only ``/__buildinfo`` route to the standard agent app so the
post-main pipeline can confirm that the app it is about to qualify is running
the build the pipeline just deployed.

The route lives here, in the fixture, and imports nothing from the runtime's
registration layer, so it cannot collide with product endpoint work.

**Why this is evidence rather than self-report.** The marker is a file inside
the deployed package. A file can be served only if the package containing it is
genuinely on disk, so a stale app cannot claim a build it is not running. An app
setting or resource tag could be changed without deploying anything, which is
precisely where a service reporting its own version stops proving anything.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import azure.functions as func
from azurefunctions.extensions.http.fastapi import JSONResponse, Request

from azure_functions_agents import create_function_app

app = create_function_app()

_APP_ROOT = Path(__file__).resolve().parent
_BUILD_INFO_PATH = _APP_ROOT / "BUILD_INFO.json"

# Content-size evidence is capped so a pathological tree cannot make the probe
# itself the slow thing being measured. Well above the ~6k entries a normal
# closure produces, and far below the platform's 65,535 ZIP-entry limit.
_MAX_SCANNED_ENTRIES = 20_000


def _load_marker() -> dict[str, Any]:
    """Read the pipeline-stamped marker, or report its absence explicitly."""
    try:
        raw = _BUILD_INFO_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"marker": "absent"}
    except OSError as error:
        return {"marker": "unreadable", "error": type(error).__name__}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"marker": "invalid"}
    if not isinstance(parsed, dict):
        return {"marker": "invalid"}
    parsed["marker"] = "present"
    return parsed


def _content_size() -> dict[str, Any]:
    """Measure deployed content against the platform's package limits.

    Reported so the pipeline can trend closure growth toward the 256 MiB /
    65,535-entry caps while there is still headroom to act, rather than
    discovering the ceiling as a deployment failure.
    """
    total_bytes = 0
    entry_count = 0
    truncated = False
    for path in _APP_ROOT.rglob("*"):
        if entry_count >= _MAX_SCANNED_ENTRIES:
            truncated = True
            break
        try:
            if not path.is_file():
                continue
            total_bytes += path.stat().st_size
        except OSError:
            # A file that vanishes or denies stat mid-scan is not worth failing
            # the probe over; it is counted as an entry and skipped for size.
            pass
        entry_count += 1
    return {
        "entry_count": entry_count,
        "total_bytes": total_bytes,
        "truncated": truncated,
    }


@app.route(
    route="__buildinfo",
    methods=["GET"],
    auth_level=func.AuthLevel.ANONYMOUS,
)
def build_info(req: Request) -> JSONResponse:
    """Return the deployed build marker plus live host facts.

    The parameter and return types must be the **FastAPI** ``Request`` and a
    FastAPI response, matching how the runtime registers every other route.
    Using ``azure.functions.HttpRequest`` here makes the worker reject the
    binding, and because indexing is all-or-nothing that single bad function
    takes down the entire app -- every agent route included, reported only as
    "No job functions found".

    ``auth_level`` is anonymous because Easy Auth is the gate: the platform is
    configured with ``requireAuthentication`` and ``Return401``, so an
    unauthenticated request never reaches this function. Adding a function key
    would impose a second, different credential that the qualification job's
    Entra token could not satisfy.
    """
    del req
    payload = {
        "build": _load_marker(),
        "runtime": {
            # Live, not stamped: with remote build nothing in the pipeline pins
            # the interpreter, so this is the only confirmation that the 3.13
            # leg actually ran on 3.13.
            "python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
            "python_micro": sys.version_info.micro,
        },
        "content": _content_size(),
    }
    return JSONResponse(content=payload, status_code=200)
