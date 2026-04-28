# Trigger Reference

This document describes all trigger types supported in `.agent.md` frontmatter and how to configure them.

## How triggers work

Each `*.agent.md` file (except `main.agent.md`) requires a `trigger` section in its YAML frontmatter. The `type` field determines the Azure Functions trigger, and all other fields are passed as parameters to the trigger decorator.

```yaml
---
trigger:
  type: <trigger_type>
  <param>: <value>
---
```

The framework calls `getattr(app, trigger_type)(**params)` to register the function, so any parameter accepted by the Azure Functions Python SDK decorator can be used.

> **Environment variable substitution**: All string values under `trigger.*` (except `type`) support `$VAR` or `%VAR%` syntax for full-string env var replacement.

---

## HTTP trigger

Exposes the agent as a REST API endpoint that returns structured JSON.

```yaml
trigger:
  type: http_trigger
  route: my-endpoint
  methods: ["POST"]
  auth_level: FUNCTION
```

| Parameter | Required | Default | Description |
|---|---|---|---|
| `route` | Yes | — | URL path for the endpoint |
| `methods` | No | `["POST"]` | HTTP methods to accept |
| `auth_level` | No | `FUNCTION` | `ANONYMOUS`, `FUNCTION`, or `ADMIN` |

Use `response_example` (top-level, not under `trigger`) to define the expected JSON output format. See [HTTP-triggered agents](../README.md#http-triggered-agents).

---

## Timer trigger

Runs the agent on a schedule (cron expression).

```yaml
trigger:
  type: timer_trigger
  schedule: "0 0 9 * * *"
```

| Parameter | Required | Default | Description |
|---|---|---|---|
| `schedule` | Yes | — | NCRONTAB expression (6-part with seconds, or 5-part — seconds are prepended automatically) |
| `run_on_startup` | No | `false` | If `true`, the function runs when the host starts |
| `use_monitor` | No | `true` | Whether to monitor the schedule for missed executions |

**Schedule examples:**
- `"0 0 9 * * *"` — every day at 9:00 AM UTC
- `"0 */5 * * * *"` — every 5 minutes
- `"0 30 14 * * 1-5"` — weekdays at 2:30 PM UTC
- `"0 9 * * *"` — 5-part cron (seconds auto-prepended → `"0 0 9 * * *"`)

Ref: [Azure Functions timer trigger](https://learn.microsoft.com/azure/azure-functions/functions-bindings-timer)

---

## Queue trigger

Triggers when a message is added to an Azure Storage queue.

```yaml
trigger:
  type: queue_trigger
  queue_name: my-queue
  connection: $STORAGE_CONNECTION
```

| Parameter | Required | Default | Description |
|---|---|---|---|
| `queue_name` | Yes | — | Name of the queue to monitor |
| `connection` | Yes | — | App setting name for the storage connection string |

Ref: [Azure Functions queue trigger](https://learn.microsoft.com/azure/azure-functions/functions-bindings-storage-queue-trigger)

---

## Blob trigger

Triggers when a blob is created or updated in Azure Storage.

```yaml
trigger:
  type: blob_trigger
  path: my-container/{name}
  connection: $STORAGE_CONNECTION
```

| Parameter | Required | Default | Description |
|---|---|---|---|
| `path` | Yes | — | Blob path pattern (e.g. `container/{name}`) |
| `connection` | Yes | — | App setting name for the storage connection string |
| `source` | No | `LogsAndContainerScan` | `EventGrid` for lower latency |

Ref: [Azure Functions blob trigger](https://learn.microsoft.com/azure/azure-functions/functions-bindings-storage-blob-trigger)

---

## Event Hub trigger

Triggers when events are sent to an Azure Event Hub.

```yaml
trigger:
  type: event_hub_message_trigger
  event_hub_name: my-hub
  connection: $EVENTHUB_CONNECTION
```

| Parameter | Required | Default | Description |
|---|---|---|---|
| `event_hub_name` | Yes | — | Name of the Event Hub |
| `connection` | Yes | — | App setting for the Event Hub connection |
| `consumer_group` | No | `$Default` | Consumer group name |
| `cardinality` | No | `ONE` | `ONE` for single events, `MANY` for batching |

Ref: [Azure Functions Event Hub trigger](https://learn.microsoft.com/azure/azure-functions/functions-bindings-event-hubs-trigger)

---

## Service Bus queue trigger

Triggers when a message is sent to a Service Bus queue.

```yaml
trigger:
  type: service_bus_queue_trigger
  queue_name: my-queue
  connection: $SERVICEBUS_CONNECTION
```

| Parameter | Required | Default | Description |
|---|---|---|---|
| `queue_name` | Yes | — | Name of the Service Bus queue |
| `connection` | Yes | — | App setting for the Service Bus connection |
| `is_sessions_enabled` | No | `false` | Enable session-aware processing |
| `cardinality` | No | `ONE` | `ONE` or `MANY` for batching |
| `auto_complete_messages` | No | `true` | Auto-complete messages after processing |

Ref: [Azure Functions Service Bus trigger](https://learn.microsoft.com/azure/azure-functions/functions-bindings-service-bus-trigger)

---

## Service Bus topic trigger

Triggers when a message is sent to a Service Bus topic subscription.

```yaml
trigger:
  type: service_bus_topic_trigger
  topic_name: my-topic
  subscription_name: my-sub
  connection: $SERVICEBUS_CONNECTION
```

| Parameter | Required | Default | Description |
|---|---|---|---|
| `topic_name` | Yes | — | Name of the Service Bus topic |
| `subscription_name` | Yes | — | Name of the subscription |
| `connection` | Yes | — | App setting for the Service Bus connection |
| `is_sessions_enabled` | No | `false` | Enable session-aware processing |
| `cardinality` | No | `ONE` | `ONE` or `MANY` for batching |

Ref: [Azure Functions Service Bus trigger](https://learn.microsoft.com/azure/azure-functions/functions-bindings-service-bus-trigger)

---

## Cosmos DB trigger

Triggers when documents are created or modified in a Cosmos DB container.

```yaml
trigger:
  type: cosmos_db_trigger
  database_name: my-db
  container_name: my-container
  connection: $COSMOSDB_CONNECTION
```

| Parameter | Required | Default | Description |
|---|---|---|---|
| `database_name` | Yes | — | Cosmos DB database name |
| `container_name` | Yes | — | Container to monitor |
| `connection` | Yes | — | App setting for the Cosmos DB connection |
| `lease_connection` | No | Same as `connection` | Connection for the lease container |
| `lease_container_name` | No | `leases` | Container for storing leases |
| `create_lease_container_if_not_exists` | No | `false` | Auto-create the lease container |
| `max_items_per_invocation` | No | — | Max documents per invocation |
| `start_from_beginning` | No | `false` | Start reading from the beginning of change history |

Ref: [Azure Functions Cosmos DB trigger](https://learn.microsoft.com/azure/azure-functions/functions-bindings-cosmosdb-v2-trigger)

---

## Event Grid trigger

Triggers in response to an Event Grid event.

```yaml
trigger:
  type: event_grid_trigger
```

No additional parameters required — Event Grid subscriptions are configured externally.

Ref: [Azure Functions Event Grid trigger](https://learn.microsoft.com/azure/azure-functions/functions-bindings-event-grid-trigger)

---

## Kafka trigger

Triggers when events are sent to a Kafka topic.

```yaml
trigger:
  type: kafka_trigger
  topic: my-topic
  broker_list: $KAFKA_BROKERS
```

| Parameter | Required | Default | Description |
|---|---|---|---|
| `topic` | Yes | — | Kafka topic to monitor |
| `broker_list` | Yes | — | Comma-separated list of Kafka brokers |
| `consumer_group` | No | — | Consumer group |
| `cardinality` | No | `ONE` | `ONE` or `MANY` for batching |
| `authentication_mode` | No | `Plain` | `Gssapi`, `Plain`, `ScramSha256`, `ScramSha512` |
| `protocol` | No | `plaintext` | Security protocol |

Ref: [Azure Functions Kafka trigger](https://learn.microsoft.com/azure/azure-functions/functions-bindings-kafka-trigger)

---

## SQL trigger

Triggers when rows change in a SQL database table.

```yaml
trigger:
  type: sql_trigger
  table_name: dbo.MyTable
  connection_string_setting: $SQL_CONNECTION
```

| Parameter | Required | Default | Description |
|---|---|---|---|
| `table_name` | Yes | — | SQL table to monitor |
| `connection_string_setting` | Yes | — | App setting for the SQL connection string |

Ref: [Azure Functions SQL trigger](https://learn.microsoft.com/azure/azure-functions/functions-bindings-azure-sql-trigger)

---

## Connector triggers

Triggers from [Azure API Connections](https://learn.microsoft.com/azure/connectors/connectors-overview) (Teams, Office 365, Dynamics, etc.) use dot notation. These require the [`azure-functions-connectors`](https://github.com/anthonychu/azure-functions-connectors-python) package (`azure-functions-agents[connectors]`).

```yaml
trigger:
  type: teams.new_channel_message_trigger
  # connector-specific params
```

| Format | Description |
|---|---|
| `teams.new_channel_message_trigger` | Teams channel message |
| `connectors.generic_trigger` | Generic connector trigger |

---

## Generic trigger (Azure Functions)

For trigger types not directly supported by the framework, use the built-in Azure Functions generic trigger. This works with any extension bundle binding.

```yaml
trigger:
  type: generic_trigger
  type: cosmosDBTrigger    # the actual binding type name
  connection: $COSMOSDB_CONNECTION
  databaseName: my-db
  containerName: my-container
```

| Parameter | Required | Default | Description |
|---|---|---|---|
| `type` (second) | Yes | — | The binding type name (as it appears in `function.json`) |

All other parameters are passed through to the binding definition. Use this when a trigger type isn't available as a dedicated decorator.

Ref: [Azure Functions custom bindings](https://learn.microsoft.com/azure/azure-functions/functions-bindings-register)

---

## Generic connector trigger

For connector triggers not covered by specific helpers (e.g., `teams.new_channel_message_trigger`), use the generic connector trigger with explicit connection and trigger parameters.

```yaml
trigger:
  type: connectors.generic_trigger
  connection_name: $CONNECTION_NAME
  trigger_identifier: <trigger-id>
```

| Parameter | Required | Default | Description |
|---|---|---|---|
| `connection_name` | Yes | — | The name of the Azure API Connection resource |
| `trigger_identifier` | Yes | — | The trigger identifier from the connector's API definition |

This is a fallback for connectors that don't have dedicated trigger methods in the connectors library.

---

## Trigger data in prompts

When any trigger fires, the agent receives a prompt containing:

1. The agent's markdown body (instructions)
2. The trigger type and serialized trigger data:

```
<agent instructions>

Triggered by: timer_trigger

Trigger data:
```json
{"past_due": false, "schedule": {...}}
```​
```

The agent uses this context to decide what actions to take.
