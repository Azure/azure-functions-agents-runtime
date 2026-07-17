# Trigger Reference

This document describes the trigger types that can be used in `.agent.md` front matter. The runtime builds on the Azure Functions Python `FunctionApp` decorator model, with a few agent-specific rules.

## How Triggers Work

Each `*.agent.md` file requires either a `trigger` section or at least one enabled `builtin_endpoints` value. The `type` field selects the trigger, and `trigger.args` is passed to the underlying Azure Functions decorator.

```yaml
---
trigger:
  type: <trigger_type>
  args:
    <param>: <value>
---
```

Runtime rules:

- Do not set `arg_name` in `trigger.args`. The runtime always injects `arg_name: trigger_data` for non-HTTP Azure Functions triggers.
- `http_trigger` is the agent-runtime name for the Azure Functions `route(...)` decorator. Use `http_trigger`, not `route`.
- Connector-triggered agents use `connector_trigger`, which maps to the Azure Functions Python `connector_trigger(...)` decorator.
- Other supported trigger types map directly to `FunctionApp.<trigger_type>(arg_name="trigger_data", **trigger.args)`.
- `timer_trigger` accepts 5-part cron expressions; the runtime prepends seconds before registration.
- String values under `trigger.*`, including `type`, follow [environment variable substitution](./front-matter-spec.md#environment-variable-substitution).

## Supported Trigger Types

| Agent `trigger.type` | Azure Functions decorator | Status | Notes |
|---|---|---|---|
| `http_trigger` | `route(...)` | Supported with runtime mapping | Agent-specific name. Requires `route` in `trigger.args`. |
| `timer_trigger` | `timer_trigger(...)` | Supported | Preferred timer trigger name. |
| `queue_trigger` | `queue_trigger(...)` | Supported | Azure Storage Queue trigger. |
| `blob_trigger` | `blob_trigger(...)` | Supported | Azure Blob Storage trigger. |
| `event_grid_trigger` | `event_grid_trigger(...)` | Supported | Event Grid trigger. |
| `event_hub_message_trigger` | `event_hub_message_trigger(...)` | Supported | Event Hubs trigger. |
| `service_bus_queue_trigger` | `service_bus_queue_trigger(...)` | Supported | Service Bus queue trigger. |
| `service_bus_topic_trigger` | `service_bus_topic_trigger(...)` | Supported | Service Bus topic subscription trigger. |
| `cosmos_db_trigger` | `cosmos_db_trigger(...)` | Supported | Cosmos DB extension bundle 4.x+ shape. |
| `cosmos_db_trigger_v3` | `cosmos_db_trigger_v3(...)` | Supported | Cosmos DB extension bundle 2.x/3.x shape. |
| `sql_trigger` | `sql_trigger(...)` | Supported | Azure SQL trigger. |
| `mysql_trigger` | `mysql_trigger(...)` | Supported | Azure Database for MySQL trigger. |
| `kafka_trigger` | `kafka_trigger(...)` | Supported | Kafka/Event Hubs Kafka endpoint trigger. |
| `dapr_binding_trigger` | `dapr_binding_trigger(...)` | Supported | Dapr input binding trigger. |
| `dapr_service_invocation_trigger` | `dapr_service_invocation_trigger(...)` | Supported | Dapr service invocation trigger. |
| `dapr_topic_trigger` | `dapr_topic_trigger(...)` | Supported | Dapr pub/sub topic trigger. |
| `generic_trigger` | `generic_trigger(...)` | Supported | Custom extension binding trigger. |
| `connector_trigger` | `connector_trigger(...)` | Supported | Generic connector trigger. |

## Unsupported Trigger Decorators

These Azure Functions Python decorators are intentionally not supported as `.agent.md` triggers.

| Do not use | Why | Use instead |
|---|---|---|
| `route` | The runtime owns HTTP handler creation and response validation. | `http_trigger` |
| `schedule` | The runtime uses only the explicit timer trigger name. | `timer_trigger` |
| `activity_trigger` | Durable Functions activity triggers do not match the agent request/response handler shape. | A regular supported trigger that calls an agent, or custom Functions code. |
| `orchestration_trigger` | Durable orchestrators require Durable-specific context handling. | Custom Durable Functions code outside `.agent.md`. |
| `entity_trigger` | Durable entities require Durable-specific context handling. | Custom Durable Functions code outside `.agent.md`. |
| `warm_up_trigger` | Warm-up triggers are host lifecycle hooks, not user/event payload triggers for agents. | Use `timer_trigger` or another event trigger for agent work. |
| `assistant_skill_trigger` | Azure Functions assistant skill triggers overlap with agent tool/MCP concepts but use a different extension contract. | Project tools, skills, or MCP servers. |
| `mcp_tool_trigger` | Runtime MCP tool endpoints are registered by `builtin_endpoints.mcp`. | `builtin_endpoints.mcp: true`. |
| `mcp_resource_trigger` | Runtime MCP resources are not authored as `.agent.md` triggers. | Built-in MCP surfaces. |
| `mcp_prompt_trigger` | Runtime MCP prompts are not authored as `.agent.md` triggers. | Built-in MCP surfaces. |
| Dotted connector trigger types such as `teams.new_channel_message_trigger` or `connectors.generic_trigger` | Dotted connector trigger resolution is not supported. | `connector_trigger` |

### Built-in endpoint authentication

The HTTP chat API registered by `builtin_endpoints` is protected via `builtin_endpoints.http_auth`. Modes: `function` (API key, default), `admin` (master key), `anonymous`, and `entra` (Entra ID / Azure AD). For `entra`, the chat routes rely on platform-level App Service Authentication (Easy Auth): the platform validates the Entra token and the runtime enforces the injected `x-ms-client-principal`. `http_auth` applies only to HTTP endpoints and does not affect the MCP endpoint (`/runtime/webhooks/mcp`), which is owned by the Functions MCP extension and always requires the MCP extension system key (`x-functions-key`). See [`front-matter-spec.md`](front-matter-spec.md#http_auth--endpoint-authentication) for the full schema and examples.

## HTTP Trigger

`http_trigger` exposes the agent as a REST API endpoint. It maps to Azure Functions `app.route(...)`, but the agent runtime owns the handler, prompt construction, session id, and response validation.

```yaml
trigger:
  type: http_trigger
  args:
    route: my-endpoint
    methods: ["POST"]
    http_auth: function
```

| Parameter | Required | Default | Description |
|---|---|---|---|
| `route` | Yes | - | URL path for the endpoint. |
| `methods` | No | `["POST"]` | HTTP methods to accept. |
| `http_auth` | No | `function` | Inbound auth policy, the same model as `builtin_endpoints.http_auth`. Accepts a string (`function`, `admin`, `anonymous`, `entra`) or an object with `mode` + optional `entra` allow-lists. |
| `auth_level` | No | `function` | **Deprecated** — use `http_auth` instead. Still accepted (`anonymous`, `function`, or `admin`); if both are set, `http_auth` wins and `auth_level` is ignored with a warning. |

### HTTP trigger authentication

`http_auth` reuses the same `EndpointAuthConfig` model as the built-in chat endpoints, so HTTP-triggered agents get identical enforcement:

- `function` (default) — Azure Functions host key check (a function or host key, `AuthLevel.FUNCTION`).
- `admin` — Azure Functions master key check (`AuthLevel.ADMIN` maps to the `_master` key, distinct from an extension system key).
- `anonymous` — no auth.
- `entra` — the route is registered anonymous at the key layer and identity is enforced in-app against the App Service Authentication (Easy Auth) `x-ms-client-principal` header, with optional tenant/audience/client-id allow-lists. Requests without a validated principal (or without verifiable Easy Auth enforcement) are rejected before the agent runs.

```yaml
trigger:
  type: http_trigger
  args:
    route: secured
    http_auth:
      mode: entra
      entra:
        tenant_id: <tenant-guid>
        allowed_audiences: ["api://my-app"]
```

See [`front-matter-spec.md`](front-matter-spec.md#http_auth--endpoint-authentication) for the full auth schema and semantics.

By default, the handler returns the agent response as `text/plain`. When `response_example` or `response_schema` is configured at the top level, the runtime instructs the model to return JSON, validates the result, and returns `application/json`.

HTTP requests can pass `x-ms-session-id`; otherwise the runtime creates a session id and returns it in the response header.

Use `response_example` or `response_schema` at the top level, not under `trigger`.

## Timer Triggers

Use `timer_trigger` for scheduled agents. The Azure Functions `schedule` alias is not supported in agent files.

```yaml
trigger:
  type: timer_trigger
  args:
    schedule: "0 0 9 * * *"
```

| Parameter | Required | Default | Description |
|---|---|---|---|
| `schedule` | Yes | - | NCRONTAB expression. 6-part with seconds, or 5-part with seconds prepended by the runtime. |
| `run_on_startup` | No | Host default | If `true`, the function runs when the host starts. |
| `use_monitor` | No | Host default | Whether the host monitors the schedule for missed executions. |
| `data_type` | No | Host default | Forwarded to the Azure Functions decorator. |

Examples:

- `"0 0 9 * * *"` - every day at 9:00 AM UTC.
- `"0 */5 * * * *"` - every 5 minutes.
- `"0 30 14 * * 1-5"` - weekdays at 2:30 PM UTC.
- `"0 9 * * *"` - 5-part cron; registered as `"0 0 9 * * *"`.

Ref: [Azure Functions timer trigger](https://learn.microsoft.com/azure/azure-functions/functions-bindings-timer)

## Storage Triggers

### Queue Trigger

Triggers when a message is added to an Azure Storage queue.

```yaml
trigger:
  type: queue_trigger
  args:
    queue_name: my-queue
    connection: AzureWebJobsStorage
```

| Parameter | Required | Description |
|---|---|---|
| `queue_name` | Yes | Name of the queue to monitor. |
| `connection` | Yes | App setting or setting collection for Azure Queue Storage. |
| `data_type` | No | Forwarded to the Azure Functions decorator. |

Ref: [Azure Functions queue trigger](https://learn.microsoft.com/azure/azure-functions/functions-bindings-storage-queue-trigger)

### Blob Trigger

Triggers when a blob is created or updated.

```yaml
trigger:
  type: blob_trigger
  args:
    path: my-container/{name}
    connection: AzureWebJobsStorage
```

| Parameter | Required | Description |
|---|---|---|
| `path` | Yes | Blob path pattern, such as `container/{name}`. |
| `connection` | Yes | App setting or setting collection for Azure Blob Storage. |
| `source` | No | `EventGrid` for Event Grid-based blob trigger; otherwise host default. |
| `data_type` | No | Forwarded to the Azure Functions decorator. |

Ref: [Azure Functions blob trigger](https://learn.microsoft.com/azure/azure-functions/functions-bindings-storage-blob-trigger)

## Eventing And Messaging Triggers

### Event Grid Trigger

Triggers in response to an Event Grid event.

```yaml
trigger:
  type: event_grid_trigger
```

| Parameter | Required | Description |
|---|---|---|
| `data_type` | No | Forwarded to the Azure Functions decorator. |

Event Grid subscriptions are configured externally.

Ref: [Azure Functions Event Grid trigger](https://learn.microsoft.com/azure/azure-functions/functions-bindings-event-grid-trigger)

### Event Hub Trigger

Triggers when events are sent to an Azure Event Hub.

```yaml
trigger:
  type: event_hub_message_trigger
  args:
    event_hub_name: my-hub
    connection: EVENTHUB_CONNECTION
```

| Parameter | Required | Description |
|---|---|---|
| `connection` | Yes | App setting or setting collection for Event Hubs. |
| `event_hub_name` | Yes | Name of the Event Hub. |
| `consumer_group` | No | Consumer group name. |
| `cardinality` | No | `one` or `many` for single-event or batch delivery. |
| `data_type` | No | Forwarded to the Azure Functions decorator. |

Ref: [Azure Functions Event Hubs trigger](https://learn.microsoft.com/azure/azure-functions/functions-bindings-event-hubs-trigger)

### Service Bus Queue Trigger

Triggers when a message is sent to a Service Bus queue.

```yaml
trigger:
  type: service_bus_queue_trigger
  args:
    queue_name: my-queue
    connection: SERVICEBUS_CONNECTION
```

| Parameter | Required | Description |
|---|---|---|
| `connection` | Yes | App setting or setting collection for Service Bus. |
| `queue_name` | Yes | Name of the queue. |
| `is_sessions_enabled` | No | Enable session-aware processing. |
| `cardinality` | No | `one` or `many` for single-message or batch delivery. |
| `auto_complete_messages` | No | Whether messages are completed automatically after processing. |
| `access_rights` | No | Forwarded to the Azure Functions decorator. |
| `data_type` | No | Forwarded to the Azure Functions decorator. |

Ref: [Azure Functions Service Bus trigger](https://learn.microsoft.com/azure/azure-functions/functions-bindings-service-bus-trigger)

### Service Bus Topic Trigger

Triggers when a message is sent to a Service Bus topic subscription.

```yaml
trigger:
  type: service_bus_topic_trigger
  args:
    topic_name: my-topic
    subscription_name: my-subscription
    connection: SERVICEBUS_CONNECTION
```

| Parameter | Required | Description |
|---|---|---|
| `connection` | Yes | App setting or setting collection for Service Bus. |
| `topic_name` | Yes | Name of the topic. |
| `subscription_name` | Yes | Name of the subscription. |
| `is_sessions_enabled` | No | Enable session-aware processing. |
| `cardinality` | No | `one` or `many` for single-message or batch delivery. |
| `auto_complete_messages` | No | Whether messages are completed automatically after processing. |
| `access_rights` | No | Forwarded to the Azure Functions decorator. |
| `data_type` | No | Forwarded to the Azure Functions decorator. |

Ref: [Azure Functions Service Bus trigger](https://learn.microsoft.com/azure/azure-functions/functions-bindings-service-bus-trigger)

### Kafka Trigger

Triggers when events are sent to a Kafka topic or an Event Hubs Kafka endpoint.

```yaml
trigger:
  type: kafka_trigger
  args:
    topic: my-topic
    broker_list: $KAFKA_BROKERS
    consumer_group: my-consumer-group
```

| Parameter | Required | Description |
|---|---|---|
| `topic` | Yes | Kafka topic to monitor. |
| `broker_list` | Yes | Comma-separated broker list. |
| `event_hub_connection_string` | No | Event Hubs connection string setting when using Kafka protocol headers. |
| `consumer_group` | No | Consumer group. |
| `cardinality` | No | `one` or `many` for single-event or batch delivery. |
| `authentication_mode` | No | Authentication mode, such as `Plain`, `Gssapi`, `ScramSha256`, or `ScramSha512`. |
| `protocol` | No | Broker protocol/security protocol. |
| `username`, `password` | No | SASL credentials when applicable. |
| `avro_schema`, `key_avro_schema`, `key_data_type` | No | Avro/key schema settings. |
| `schema_registry_url`, `schema_registry_username`, `schema_registry_password` | No | Schema registry settings. |
| `o_auth_bearer_*` | No | OAuth bearer settings supported by the Azure Functions decorator. |
| `ssl_*` | No | SSL certificate/key settings supported by the Azure Functions decorator. |
| `lag_threshold` | No | Scaling estimate threshold. |
| `data_type` | No | Forwarded to the Azure Functions decorator. |

Ref: [Azure Functions Kafka trigger](https://learn.microsoft.com/azure/azure-functions/functions-bindings-kafka-trigger)

## Database Triggers

### Cosmos DB Trigger

Use `cosmos_db_trigger` for Cosmos DB extension bundle 4.x and later.

```yaml
trigger:
  type: cosmos_db_trigger
  args:
    connection: COSMOSDB_CONNECTION
    database_name: my-db
    container_name: my-container
```

| Parameter | Required | Description |
|---|---|---|
| `connection` | Yes | App setting or setting collection for Cosmos DB. |
| `database_name` | Yes | Database containing the monitored container. |
| `container_name` | Yes | Container to monitor. |
| `lease_connection` | No | Connection for the lease container. |
| `lease_database_name` | No | Lease database name. |
| `lease_container_name` | No | Lease container name. |
| `create_lease_container_if_not_exists` | No | Auto-create lease container. |
| `leases_container_throughput` | No | Throughput for auto-created lease container. |
| `lease_container_prefix` | No | Prefix for leases. |
| `feed_poll_delay` | No | Poll delay in milliseconds. |
| `lease_acquire_interval`, `lease_expiration_interval`, `lease_renew_interval` | No | Lease timing settings. |
| `max_items_per_invocation` | No | Max documents per invocation. |
| `start_from_beginning` | No | Start from the beginning of change history. |
| `start_from_time` | No | ISO 8601 start time for initial trigger state. |
| `preferred_locations` | No | Preferred Cosmos DB account regions. |
| `data_type` | No | Forwarded to the Azure Functions decorator. |

Ref: [Azure Functions Cosmos DB trigger](https://learn.microsoft.com/azure/azure-functions/functions-bindings-cosmosdb-v2-trigger)

### Cosmos DB Trigger V3

Use `cosmos_db_trigger_v3` for older extension bundle 2.x/3.x projects.

```yaml
trigger:
  type: cosmos_db_trigger_v3
  args:
    database_name: my-db
    collection_name: my-collection
    connection_string_setting: COSMOSDB_CONNECTION
```

| Parameter | Required | Description |
|---|---|---|
| `database_name` | Yes | Database containing the monitored collection. |
| `collection_name` | Yes | Collection to monitor. |
| `connection_string_setting` | Yes | App setting for the Cosmos DB connection string. |
| `lease_collection_name` | No | Lease collection name. |
| `lease_connection_string_setting` | No | Lease connection string setting. |
| `lease_database_name` | No | Lease database name. |
| `create_lease_collection_if_not_exists` | No | Auto-create lease collection. |
| `leases_collection_throughput` | No | Throughput for auto-created lease collection. |
| `lease_collection_prefix` | No | Prefix for leases. |
| `checkpoint_interval`, `checkpoint_document_count` | No | Checkpoint settings. |
| `feed_poll_delay` | No | Poll delay in milliseconds. |
| `lease_renew_interval`, `lease_acquire_interval`, `lease_expiration_interval` | No | Lease timing settings. |
| `max_items_per_invocation` | No | Max documents per invocation. |
| `start_from_beginning` | No | Start from the beginning of change history. |
| `preferred_locations` | No | Preferred Cosmos DB account regions. |
| `data_type` | No | Forwarded to the Azure Functions decorator. |

### SQL Trigger

Triggers when rows change in a SQL database table.

```yaml
trigger:
  type: sql_trigger
  args:
    table_name: dbo.MyTable
    connection_string_setting: SQL_CONNECTION
```

| Parameter | Required | Description |
|---|---|---|
| `table_name` | Yes | SQL table to monitor. |
| `connection_string_setting` | Yes | App setting for the SQL connection string. |
| `leases_table_name` | No | Table used to store leases. |
| `data_type` | No | Forwarded to the Azure Functions decorator. |

Ref: [Azure Functions SQL trigger](https://learn.microsoft.com/azure/azure-functions/functions-bindings-azure-sql-trigger)

### MySQL Trigger

Triggers when rows change in a MySQL table.

```yaml
trigger:
  type: mysql_trigger
  args:
    table_name: my_table
    connection_string_setting: MYSQL_CONNECTION
```

| Parameter | Required | Description |
|---|---|---|
| `table_name` | Yes | MySQL table to monitor. |
| `connection_string_setting` | Yes | App setting for the MySQL connection string. |
| `leases_table_name` | No | Table used to store leases. |
| `data_type` | No | Forwarded to the Azure Functions decorator. |

## Dapr Triggers

Dapr triggers require the corresponding Azure Functions Dapr extension configuration.

### Dapr Binding Trigger

```yaml
trigger:
  type: dapr_binding_trigger
  args:
    binding_name: input-binding
```

| Parameter | Required | Description |
|---|---|---|
| `binding_name` | Yes | Name of the Dapr input binding. |
| `data_type` | No | Forwarded to the Azure Functions decorator. |

Ref: [Azure Functions Dapr binding trigger](https://learn.microsoft.com/azure/azure-functions/functions-bindings-dapr-trigger-input-binding)

### Dapr Service Invocation Trigger

```yaml
trigger:
  type: dapr_service_invocation_trigger
  args:
    method_name: summarize
```

| Parameter | Required | Description |
|---|---|---|
| `method_name` | Yes | Dapr service invocation method name. |
| `data_type` | No | Forwarded to the Azure Functions decorator. |

Ref: [Azure Functions Dapr service invocation trigger](https://learn.microsoft.com/azure/azure-functions/functions-bindings-dapr-trigger-service-invocation)

### Dapr Topic Trigger

```yaml
trigger:
  type: dapr_topic_trigger
  args:
    pub_sub_name: pubsub
    topic: reports
```

| Parameter | Required | Description |
|---|---|---|
| `pub_sub_name` | Yes | Name of the Dapr pub/sub component. |
| `topic` | Yes | Topic name. |
| `route` | No | Trigger route; Azure Functions defaults it from the topic when omitted. |
| `data_type` | No | Forwarded to the Azure Functions decorator. |

Ref: [Azure Functions Dapr topic trigger](https://learn.microsoft.com/azure/azure-functions/functions-bindings-dapr-trigger-topic)

## Generic Trigger

Use `generic_trigger` for trigger types provided by an extension bundle when no dedicated Python decorator is available.

```yaml
trigger:
  type: generic_trigger
  args:
    type: customTrigger
    connection: CUSTOM_CONNECTION
    customProperty: value
```

| Parameter | Required | Description |
|---|---|---|
| `type` | Yes | Binding type name as it appears in `function.json`. |
| `data_type` | No | Forwarded to the Azure Functions decorator. |

All other properties in `args` are forwarded to the generic binding definition.

Ref: [Azure Functions custom bindings](https://learn.microsoft.com/azure/azure-functions/functions-bindings-register)

## Connector Triggers

Connector-triggered agents use `trigger.type: connector_trigger`. The runtime uses the Azure Functions Python `connector_trigger(...)` decorator.

```yaml
trigger:
  type: connector_trigger
```

Connector actions that an agent calls as tools should be exposed through connector-backed MCP servers in `mcp.json`; connector triggers are only for invoking an agent when an external connector event occurs.

## Trigger Payloads

For non-HTTP triggers, the agent's markdown body is passed separately as instructions. The per-invocation prompt contains the trigger type and serialized trigger data:

````
Triggered by: timer_trigger

Trigger data:
```json
{"past_due": false, "schedule": {...}}
```
````

HTTP-triggered agents use the same split: markdown body as instructions, request body data in the prompt.

````
HTTP request data:
```json
{"hello": "world"}
```
````

The agent uses this context plus its instructions to decide what actions to take.