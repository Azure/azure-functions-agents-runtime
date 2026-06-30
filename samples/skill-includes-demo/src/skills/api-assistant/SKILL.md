---
name: api-assistant
description: Comprehensive documentation for the Widget API including endpoints, error codes, and examples.
---

# Widget API Reference

This skill provides complete documentation for working with the Widget API.

When you need detailed information about the API, use the `read_skill_resource` tool
to read files from the `references/` and `examples/` directories.

## Available Resources

- `references/endpoints.md` - Full API endpoint documentation
- `references/error-codes.md` - Error code reference and troubleshooting
- `examples/requests.md` - Example API requests with curl commands

## Quick Reference

**Base URL:** `https://api.example.com/v2`

**Common Endpoints:**
- `GET /widgets` - List all widgets
- `GET /widgets/{id}` - Get widget by ID
- `POST /widgets` - Create a new widget

## Best Practices

- Always include the `Content-Type: application/json` header for POST/PUT requests
- Use pagination parameters (`page`, `limit`) for list endpoints
- Handle rate limiting by implementing exponential backoff
- Cache GET responses when appropriate to reduce API calls

For detailed endpoint documentation, read `references/endpoints.md`.
For error handling, read `references/error-codes.md`.
