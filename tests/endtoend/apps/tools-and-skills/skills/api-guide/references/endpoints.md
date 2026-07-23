# Widget API Endpoints

- `GET /widgets` — list all widgets. Supports `page` and `limit` query params.
- `GET /widgets/{id}` — fetch a single widget by id.
- `POST /widgets` — create a widget. Body: `{ "name": string, "color": string }`.
- `DELETE /widgets/{id}` — delete a widget by id.

All endpoints return JSON and require a bearer token.
