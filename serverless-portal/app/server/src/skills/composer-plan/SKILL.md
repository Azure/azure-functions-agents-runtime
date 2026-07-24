---
name: composer-plan
purpose: Turn a plain-English app description into a workflow graph of runtime components.
model: any (provider-agnostic — see ../../llm)
---

# Composer Plan skill

Translates a user's plain-English description of an app into a **workflow graph**
of `azurefunctions-agents-runtime` components — triggers, agents, tools, skills,
MCP servers, and outputs — that the portal renders, lets the user edit, and
compiles to a deployable Azure Functions project.

This skill is **model-independent**: it is only the prompt template
([`prompt.md`](prompt.md)) plus the component catalog
([`components.json`](components.json)). The generator renders the prompt with the
catalog and the user's request and sends it to whichever model provider is
configured under [`../../llm`](../../llm/provider.js). Editing this skill changes
*what* the planner knows and *how* it's asked; it never changes *which* model runs.

## When to use

- The user describes an outcome ("when an email arrives, classify it and open a
  ticket") and wants a first-draft workflow they can edit.

## Contract

- **Input:** the user's plain-English request (as the user message) and the
  component catalog (injected into the system prompt).
- **Output:** a single JSON object `{ nodes, edges, inputs }` following the
  workflow document schema. No prose, no code fences.
