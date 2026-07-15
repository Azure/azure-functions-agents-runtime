"""Serverless Agent Portal — FastAPI backend.

Scope (v0.1): the **create-agent flow** with Azure Blob Storage persistence and
an editor for the generated ``*.agent.md`` file. See ``serverless-portal/requirements.md``.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import storage
from .agent_md import build_agent_md, is_valid_agent_name, parse_frontmatter

app = FastAPI(title="Serverless Agent Portal", version="0.1.0")

# Same-origin serves the UI, but allow localhost during dev with a separate front end.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8080", "http://127.0.0.1:8080"],
    allow_methods=["GET", "POST", "PUT"],
    allow_headers=["*"],
)

_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class CreateAgentRequest(BaseModel):
    name: str = Field(..., description="Agent id / slug (lowercase, digits, hyphens).")
    description: str = ""
    instructions: str = ""
    builtin_endpoints: bool = True


class UpdateAgentRequest(BaseModel):
    content: str = Field(..., description="Full raw *.agent.md content.")


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@app.get("/api/health")
def health() -> dict[str, object]:
    return {
        "status": "ok",
        "storage": storage.storage_backend(),
        "project": storage._project(),
        "environment": storage._environment(),
        "container": storage._container_name(),
    }


@app.get("/api/agents")
def list_agents() -> list[dict[str, object]]:
    return [
        {
            "name": a.name,
            "displayName": a.display_name,
            "description": a.description,
            "trigger": a.trigger,
            "builtinEndpoints": a.builtin_endpoints,
            "lastModified": a.last_modified.isoformat() if a.last_modified else None,
            "size": a.size,
        }
        for a in storage.list_agents()
    ]


@app.post("/api/agents", status_code=201)
def create_agent(req: CreateAgentRequest) -> dict[str, object]:
    name = req.name.strip().lower()
    if not is_valid_agent_name(name):
        raise HTTPException(
            status_code=422,
            detail="Name must be lowercase letters, digits, or hyphens (1-40 chars).",
        )
    content = build_agent_md(
        name=name,
        description=req.description.strip(),
        instructions=req.instructions,
        builtin_endpoints=req.builtin_endpoints,
    )
    try:
        storage.create_agent(name, content)
    except storage.AgentExistsError:
        raise HTTPException(status_code=409, detail=f"Agent '{name}' already exists.")
    return {"name": name, "content": content}


@app.get("/api/agents/{name}")
def get_agent(name: str) -> dict[str, object]:
    if not is_valid_agent_name(name):
        raise HTTPException(status_code=422, detail="Invalid agent name.")
    try:
        content = storage.get_agent(name)
    except storage.AgentNotFoundError:
        raise HTTPException(status_code=404, detail=f"Agent '{name}' not found.")
    front, body = parse_frontmatter(content)
    return {"name": name, "content": content, "frontmatter": front, "body": body}


@app.put("/api/agents/{name}")
def update_agent(name: str, req: UpdateAgentRequest) -> dict[str, object]:
    if not is_valid_agent_name(name):
        raise HTTPException(status_code=422, detail="Invalid agent name.")
    # Basic guard: the content must parse as front matter + body.
    front, _ = parse_frontmatter(req.content)
    if not front:
        raise HTTPException(
            status_code=422,
            detail="Content must start with a YAML front matter block (--- ... ---).",
        )
    try:
        storage.update_agent(name, req.content)
    except storage.AgentNotFoundError:
        raise HTTPException(status_code=404, detail=f"Agent '{name}' not found.")
    return {"name": name, "content": req.content, "frontmatter": front}


# ---------------------------------------------------------------------------
# Static UI (mounted last so /api/* takes precedence)
# ---------------------------------------------------------------------------


@app.get("/")
def index() -> FileResponse:
    return FileResponse(_STATIC_DIR / "agents.html")


app.mount("/", StaticFiles(directory=_STATIC_DIR, html=True), name="static")
