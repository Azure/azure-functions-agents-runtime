from __future__ import annotations

import base64
import hashlib
import json
import os
from typing import Any

import uvicorn
from mcp.server.fastmcp import Context, FastMCP

mcp = FastMCP("obo-whoami")


def _base64url_decode(data: str) -> bytes:
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded)


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) < 2:
        return {"error": "invalid_jwt_format"}

    try:
        payload_json = _base64url_decode(parts[1]).decode("utf-8")
        payload = json.loads(payload_json)
        if isinstance(payload, dict):
            return payload
        return {"error": "invalid_payload_type"}
    except Exception as exc:  # pragma: no cover - best effort decoding
        return {"error": f"decode_failed: {exc}"}


def _request_headers(ctx: Context) -> dict[str, str]:
    request_context = ctx.request_context
    if request_context is None:
        return {}

    request = getattr(request_context, "request", None)
    if request is None:
        return {}

    headers = getattr(request, "headers", None)
    if headers is None:
        return {}

    try:
        return {str(k).lower(): str(v) for k, v in headers.items()}
    except Exception:  # pragma: no cover - defensive
        return {}


@mcp.tool(name="whoami", description="Return identity claims from inbound bearer token")
def whoami(ctx: Context) -> dict[str, Any]:
    """Echoes token identity details to validate OBO pass-through."""
    headers = _request_headers(ctx)
    auth_header = headers.get("authorization", "")

    if not auth_header.lower().startswith("bearer "):
        return {
            "auth_present": bool(auth_header),
            "token_present": False,
            "error": "missing_or_non_bearer_authorization_header",
            "observed_headers": sorted(list(headers.keys())),
        }

    token = auth_header.split(" ", 1)[1].strip()
    claims = _decode_jwt_payload(token)

    # Surface only common identity/audience fields for quick validation.
    selected_claims = {
        "oid": claims.get("oid"),
        "sub": claims.get("sub"),
        "aud": claims.get("aud"),
        "tid": claims.get("tid"),
        "azp": claims.get("azp"),
        "appid": claims.get("appid"),
        "upn": claims.get("upn"),
        "preferred_username": claims.get("preferred_username"),
        "scp": claims.get("scp"),
        "roles": claims.get("roles"),
    }

    return {
        "auth_present": True,
        "token_present": True,
        "token_sha256_12": hashlib.sha256(token.encode("utf-8")).hexdigest()[:12],
        "claims": selected_claims,
        "raw_claims": claims,
    }


app = mcp.streamable_http_app()


if __name__ == "__main__":
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host=host, port=port)
