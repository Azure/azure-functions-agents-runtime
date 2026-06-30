### GET /widgets

List all widgets with optional filtering.

**Query Parameters:**
- `status` (optional): Filter by status (`active`, `inactive`, `archived`)
- `page` (optional): Page number for pagination (default: 1)
- `limit` (optional): Items per page (default: 20, max: 100)

**Response:** Array of widget objects

---

### GET /widgets/{id}

Get a specific widget by ID.

**Path Parameters:**
- `id` (required): Widget UUID

**Response:** Single widget object

---

### POST /widgets

Create a new widget.

**Request Body:**
```json
{
  "name": "string (required)",
  "description": "string (optional)",
  "type": "string (required): basic|premium|enterprise",
  "settings": {
    "enabled": "boolean (default: true)",
    "priority": "integer (1-10, default: 5)"
  }
}
```

**Response:** Created widget object with generated `id`

---

### PUT /widgets/{id}

Update an existing widget.

**Path Parameters:**
- `id` (required): Widget UUID

**Request Body:** Same as POST (all fields optional for partial update)

**Response:** Updated widget object

---

### DELETE /widgets/{id}

Delete a widget (soft delete - moves to archived status).

**Path Parameters:**
- `id` (required): Widget UUID

**Response:** 204 No Content
