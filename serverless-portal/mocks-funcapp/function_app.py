"""Azure Functions app that serves the static Serverless Agent Portal mockups.

A single anonymous, catch-all HTTP route returns the files under ``./content``
(copied from ``../mocks`` at deploy time by ``deploy.ps1`` / ``deploy.sh``).
This turns the static mockups into a shareable URL:

    https://<app-name>.azurewebsites.net/

The mockups are illustrative only — there is no backend, data, or auth here on
purpose, so anyone with the URL can view them.
"""

from __future__ import annotations

import mimetypes
import os

import azure.functions as func

app = func.FunctionApp()

# Directory holding the static site. Populated from ../mocks by the deploy
# scripts and kept out of source control via .gitignore.
CONTENT_ROOT = os.path.realpath(os.path.join(os.path.dirname(__file__), "content"))

# Content types Python's mimetypes module misses or reports inconsistently.
_EXTRA_TYPES = {
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".json": "application/json",
    ".css": "text/css",
    ".html": "text/html",
    ".svg": "image/svg+xml",
    ".md": "text/markdown",
    ".webmanifest": "application/manifest+json",
}

_CHARSET_TYPES = {
    "application/json",
    "application/manifest+json",
    "image/svg+xml",
}


def _resolve(path: str) -> str | None:
    """Map a request path to a safe absolute file path inside CONTENT_ROOT.

    Returns ``None`` when the path escapes CONTENT_ROOT or no file exists.
    """
    rel = (path or "").lstrip("/")
    if rel == "" or rel.endswith("/"):
        rel += "index.html"

    candidate = os.path.realpath(os.path.join(CONTENT_ROOT, rel))

    # Prevent path traversal: the resolved path must stay within CONTENT_ROOT.
    if candidate != CONTENT_ROOT and not candidate.startswith(CONTENT_ROOT + os.sep):
        return None

    if os.path.isdir(candidate):
        candidate = os.path.join(candidate, "index.html")

    return candidate if os.path.isfile(candidate) else None


@app.function_name("StaticSite")
@app.route(route="{*path}", methods=[func.HttpMethod.GET], auth_level=func.AuthLevel.ANONYMOUS)
def static_site(req: func.HttpRequest) -> func.HttpResponse:
    file_path = _resolve(req.route_params.get("path", ""))
    if file_path is None:
        return func.HttpResponse("404 — Not found", status_code=404, mimetype="text/plain")

    ext = os.path.splitext(file_path)[1].lower()
    content_type = _EXTRA_TYPES.get(ext) or mimetypes.guess_type(file_path)[0] or "application/octet-stream"
    if content_type.startswith("text/") or content_type in _CHARSET_TYPES:
        content_type = f"{content_type}; charset=utf-8"

    with open(file_path, "rb") as fh:
        body = fh.read()

    return func.HttpResponse(
        body=body,
        status_code=200,
        mimetype=content_type,
        headers={"Cache-Control": "no-cache"},
    )
