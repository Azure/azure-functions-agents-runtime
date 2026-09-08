---
name: data-driven-workflows
description: Grammar for for_each over an upstream runtime array and per-item when. Irrelevant to fixed task lists, parallel branches, waits, or ordinary DAGs.
---

# Data-driven workflows

Use this skill when a workflow plan must decide at runtime whether work should
run or must apply the same tool or Sub Agent task to a collection discovered by
an earlier task.

## Collection fan-out

`for_each` is allowed on a `tool` or `sub_agent` task only, never `wait`. Its
value must be one full reference to an upstream JSON array:

```json
{
  "id": "inspect",
  "type": "tool",
  "tool": "inspect_incident",
  "depends_on": ["discover"],
  "for_each": "${discover.result.items}",
  "args": {
    "incident": "${item}",
    "source_index": "${index}"
  }
}
```

Inside that task's value fields (`args`, a Sub Agent's `task`, and `when.ref`),
use these iteration locals:

- `${item}` for the whole element
- `${item.path.to.field}` for a field within the element
- `${index}` for its zero-based source position

The locals are valid only inside a `for_each` task. `item` and `index` are reserved task ids
and must never be authored as task `id` values. Keep the target tool/agent name static; only
value fields vary per item.

## Runtime conditions

`when` is a constrained predicate:

```json
{
  "ref": "${item.requires_action}",
  "operator": "equals",
  "value": true
}
```

The exact shape is:

```text
{"ref": "${...}", "operator": "equals" | "not_equals", "value": <scalar>}
```

`ref` must be one full upstream reference or, during iteration, one full
iteration-local reference. `value` must be a JSON scalar: null, boolean, number,
or string. Comparison uses exact typed equality only. There is no coercion,
truthiness, ordering, regex, boolean composition, or function call.

The condition is evaluated before executable `args` or `task` templates are
resolved. A false condition schedules no Activity or timer and produces a null
result. A skip does not propagate: each downstream task that belongs to the
conditional branch must declare its own `when`.

## Fan-in and limits

The logical `for_each` task exposes one array of `{index, status, result}`
envelopes in source order. Skipped positions remain in the array with a null
result. Depend on the logical task id and consume the full aggregate with
`${node_id.result}`; individual runtime instances cannot be referenced.

Use collections that are already bounded. Filter or cap the array upstream so
materialized items stay within workflow node limits and runnable instances stay
within workflow parallelism limits.
