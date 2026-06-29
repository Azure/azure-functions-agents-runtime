### Create a Basic Widget

```bash
curl -X POST https://api.example.com/widgets \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -d '{
    "name": "My First Widget",
    "type": "basic",
    "description": "A simple widget for testing"
  }'
```

**Response:**
```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "name": "My First Widget",
  "type": "basic",
  "description": "A simple widget for testing",
  "status": "active",
  "settings": {
    "enabled": true,
    "priority": 5
  },
  "created_at": "2024-01-15T10:30:00Z",
  "updated_at": "2024-01-15T10:30:00Z"
}
```

---

### List Widgets with Filtering

```bash
curl "https://api.example.com/widgets?status=active&limit=10" \
  -H "Authorization: Bearer YOUR_API_KEY"
```

---

### Update Widget Settings

```bash
curl -X PUT https://api.example.com/widgets/550e8400-e29b-41d4-a716-446655440000 \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -d '{
    "settings": {
      "priority": 10,
      "enabled": false
    }
  }'
```

---

### Python Example

```python
import requests

API_BASE = "https://api.example.com"
API_KEY = "your-api-key"

headers = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json"
}

# Create a widget
response = requests.post(
    f"{API_BASE}/widgets",
    headers=headers,
    json={
        "name": "Python Widget",
        "type": "premium",
        "settings": {"priority": 8}
    }
)
widget = response.json()
print(f"Created widget: {widget['id']}")

# List all active widgets
response = requests.get(
    f"{API_BASE}/widgets",
    headers=headers,
    params={"status": "active"}
)
widgets = response.json()
print(f"Found {len(widgets)} active widgets")
```
