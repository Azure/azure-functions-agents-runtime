# Example Widget API Requests

List widgets:

```bash
curl -H "Authorization: Bearer $TOKEN" https://api.example.test/v1/widgets
```

Create a widget:

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" \
  -d '{"name": "gadget", "color": "blue"}' \
  https://api.example.test/v1/widgets
```
