---
name: api-assistant
description: Comprehensive documentation for the Widget API including endpoints, error codes, and examples.
---

# Widget API Reference

This skill provides complete documentation for working with the Widget API.

## API Endpoints

[endpoints.md](./references/endpoints.md)

## Error Handling

[error-codes.md](./references/error-codes.md)

## Example Requests

[requests.md](./examples/requests.md)

## Best Practices

- Always include the `Content-Type: application/json` header for POST/PUT requests
- Use pagination parameters (`page`, `limit`) for list endpoints
- Handle rate limiting by implementing exponential backoff
- Cache GET responses when appropriate to reduce API calls
