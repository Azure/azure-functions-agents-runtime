### HTTP Status Codes

| Code | Meaning | When It Occurs |
|------|---------|----------------|
| 200 | OK | Successful GET, PUT requests |
| 201 | Created | Successful POST request |
| 204 | No Content | Successful DELETE request |
| 400 | Bad Request | Invalid request body or parameters |
| 401 | Unauthorized | Missing or invalid API key |
| 403 | Forbidden | Valid API key but insufficient permissions |
| 404 | Not Found | Widget with specified ID doesn't exist |
| 409 | Conflict | Widget with same name already exists |
| 422 | Unprocessable Entity | Valid JSON but failed validation |
| 429 | Too Many Requests | Rate limit exceeded |
| 500 | Internal Server Error | Server-side error |

### Error Response Format

All error responses follow this structure:

```json
{
  "error": {
    "code": "ERROR_CODE",
    "message": "Human-readable description",
    "details": [
      {
        "field": "name",
        "issue": "Field is required"
      }
    ]
  }
}
```

### Common Error Codes

- `INVALID_REQUEST`: Request body is malformed or missing required fields
- `WIDGET_NOT_FOUND`: The specified widget ID does not exist
- `DUPLICATE_WIDGET`: A widget with the same name already exists
- `INVALID_TYPE`: The widget type is not one of: basic, premium, enterprise
- `RATE_LIMITED`: Too many requests; retry after the time specified in `Retry-After` header
- `VALIDATION_FAILED`: One or more fields failed validation rules
