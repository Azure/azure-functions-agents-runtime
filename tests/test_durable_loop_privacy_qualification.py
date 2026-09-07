"""Tests for the durable-loop privacy qualification scanner."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from eng.scripts import durable_loop_privacy_qualification as privacy
from eng.scripts.durable_loop_privacy_qualification import (
    AggregateObservation,
    Batch,
    BlobSnapshot,
    CanarySet,
    DuplicateKeyError,
    FixtureLiveAdapter,
    InputValidationError,
    MatchSummary,
    OperatorInput,
    PrivacyMatcher,
    QueueSnapshot,
    SandboxSnapshot,
    ScanAggregate,
    ScanResult,
    ScanStatus,
    TableSnapshot,
    TimingConfig,
    build_kusto_aggregate_query,
    classify_taskhub_table,
    inconclusive_from_exception,
    is_taskhub_container,
    is_taskhub_queue,
    load_bounded_json_bytes,
    main,
    parse_operator_input,
    render_aggregate,
    render_result,
    run_scan,
    scan_apim_diagnostics,
    scan_content_blobs,
    scan_queues,
    scan_recursive,
    scan_sandbox_inventory,
    scan_tables,
    scan_taskhub_blobs,
)


def _canary_mapping() -> dict[str, list[str]]:
    return {
        "prompt": ["PROMPT-CANARY-ALPHA"],
        "human": ["HUMAN-CANARY-BRAVO"],
        "tool_argument": ["TOOL-ARG-CANARY-CHARLIE"],
        "tool_result": ["TOOL-RESULT-CANARY-DELTA"],
        "mcp_result": ["MCP-RESULT-CANARY-ECHO"],
        "stdout_stderr": ["STDIO-CANARY-FOXTROT"],
        "reasoning": ["REASONING-CANARY-GOLF"],
        "synthetic_credential": ["CREDENTIAL-CANARY-HOTEL"],
        "synthetic_blob_sas_url": ["BLOB-SAS-CANARY-INDIA"],
    }


def _canaries() -> CanarySet:
    return CanarySet(
        by_category={key: tuple(values) for key, values in _canary_mapping().items()},
        exact_ids=("provider-exact-juliet", "call-exact-kilo", "sandbox-exact-lima"),
    )


def _operator_payload(fixtures: dict[str, Any]) -> dict[str, Any]:
    return {
        "canaries": _canary_mapping(),
        "exact_ids": {
            "provider": ["provider-exact-juliet"],
            "call": ["call-exact-kilo"],
            "sandbox": ["sandbox-exact-lima"],
        },
        "sandbox_selector": {
            "owner_kind": "function_app",
            "app_hash": "a1-private-app-hash",
        },
        "fixtures": fixtures,
    }


def _common_diagnostic() -> dict[str, Any]:
    return {
        "properties": {
            "alwaysLog": "allErrors",
            "backend": {
                "request": {"body": {"bytes": 0}, "headers": ["traceparent"]},
                "response": {"body": {"bytes": 0}, "headers": ["request-id"]},
            },
            "frontend": {
                "request": {
                    "body": {"bytes": 0},
                    "headers": ["traceparent", "x-af-operation-id"],
                },
                "response": {"body": {"bytes": 0}, "headers": ["request-id"]},
            },
            "logClientIp": False,
            "sampling": {"percentage": 100, "samplingType": "fixed"},
        }
    }


def _control_diagnostic() -> dict[str, Any]:
    return {
        "properties": {
            "backend": {
                "request": {"body": None, "headers": None},
                "response": None,
            },
            "frontend": {
                "request": {"body": None, "headers": None},
                "response": None,
            },
            "logClientIp": False,
            "sampling": {"percentage": 0, "samplingType": "fixed"},
        }
    }


def _healthy_fixtures() -> dict[str, Any]:
    body = _canary_mapping()["prompt"][0].encode()
    digest = hashlib.sha256(body).hexdigest()
    return {
        "tables": [
            {
                "name": "PrivateHubInstances",
                "entities": [
                    {
                        "PartitionKey": "hub",
                        "RowKey": "instance-1",
                        "RuntimeStatus": "Completed",
                        "CreatedTime": "2026-09-07T10:00:00Z",
                        "Input": "ordinary input",
                        "Output": "ordinary output",
                    }
                ],
            },
            {
                "name": "PrivateHubHistory",
                "entities": [
                    {
                        "PartitionKey": "instance-1",
                        "RowKey": "event-1",
                        "EventType": "TaskCompleted",
                        "Timestamp": "2026-09-07T10:01:00Z",
                        "Result": "ordinary result",
                    }
                ],
            },
            {
                "name": "PrivateHubEntities",
                "entities": [
                    {
                        "PartitionKey": "entity",
                        "RowKey": "entity-1",
                        "State": "ordinary state",
                    }
                ],
            },
        ],
        "blobs": [
            {
                "container": "privatehub-largemessages",
                "name": "messages/envelope-1.json",
                "metadata": {"kind": "history"},
                "headers": {"content_type": "application/json"},
                "body": '{"Input":"ordinary large message"}',
            },
            {
                "container": "durable-loop-content",
                "name": digest,
                "metadata": {
                    "object_class": "prompt",
                    "sha256": digest,
                    "byte_count": str(len(body)),
                },
                "headers": {
                    "content_type": "application/octet-stream",
                    "content_length": len(body),
                },
                "body": body.decode(),
            },
        ],
        "queues": [
            {
                "name": "privatehub-control-00",
                "metadata": {"kind": "control"},
                "approximate_message_count": 0,
            },
            {
                "name": "privatehub-workitems",
                "metadata": {},
                "approximate_message_count": 0,
            },
        ],
        "app_insights": {"scanned": 12, "violations": 0},
        "log_analytics": {"scanned": 14, "violations": 0},
        "apim_logs": {"scanned": 8, "violations": 0},
        "apim_diagnostics": {
            "durable-agent-loop-model": [_common_diagnostic()],
            "durable-agent-loop-model-control": [_control_diagnostic()],
            "durable-agent-loop-mcp": [_common_diagnostic()],
        },
        "sandbox_inventory": [],
    }


def _operator(fixtures: dict[str, Any] | None = None) -> OperatorInput:
    return parse_operator_input(
        _operator_payload(_healthy_fixtures() if fixtures is None else fixtures),
        require_resources=False,
    )


def _timing() -> TimingConfig:
    return TimingConfig(
        start=datetime(2026, 9, 7, 9, tzinfo=UTC),
        end=datetime(2026, 9, 7, 11, tzinfo=UTC),
        telemetry_lag_retries=0,
        telemetry_lag_seconds=0,
        timeout_seconds=30,
        late_scan_seconds=0,
        sandbox_wait_seconds=0,
        sandbox_poll_seconds=1,
    )


def test_json_loader_rejects_duplicate_keys_without_echoing_them() -> None:
    with pytest.raises(DuplicateKeyError, match="duplicate_json_key"):
        load_bounded_json_bytes(b'{"canaries":{},"canaries":{"secret":"value"}}')


def test_operator_input_requires_all_canary_categories() -> None:
    payload = _operator_payload(_healthy_fixtures())
    del payload["canaries"]["reasoning"]

    with pytest.raises(InputValidationError, match="canary_categories_invalid"):
        parse_operator_input(payload, require_resources=False)


def test_operator_input_rejects_duplicate_canary_values_across_categories() -> None:
    payload = _operator_payload(_healthy_fixtures())
    payload["canaries"]["human"] = payload["canaries"]["prompt"]

    with pytest.raises(InputValidationError, match="canary_value_invalid"):
        parse_operator_input(payload, require_resources=False)


def test_live_resources_allow_strict_private_environment_overrides() -> None:
    payload = _operator_payload(_healthy_fixtures())
    payload["resources"] = {
        "subscription_id": "00000000-0000-0000-0000-000000000000",
        "resource_group": "private-loop-rg",
        "storage_account": "privateloopstorage",
        "apim_apis": ["private-model", "private-control", "private-mcp"],
        "model_control_api": "private-control",
    }

    operator = parse_operator_input(payload, require_resources=True)

    assert operator.resources is not None
    assert operator.resources.resource_group == "private-loop-rg"
    assert operator.resources.apim_apis == (
        "private-model",
        "private-control",
        "private-mcp",
    )
    assert operator.resources.model_control_api == "private-control"


@pytest.mark.parametrize(
    ("value", "rule"),
    [
        ("provider resp_1234567890abcdef persisted", "provider_response_id"),
        ("provider call_1234567890abcdef persisted", "provider_call_id"),
        ("Authorization: Bearer abcdefghijklmnop", "bearer_or_jwt"),
        (
            "eyJabcdefghijk.abcdefghijklmnop.abcdefghijklmnop",
            "bearer_or_jwt",
        ),
        ("api-key: abcdefghijklmnop", "api_or_subscription_key"),
        ("subscription-key=abcdefghijklmnop", "api_or_subscription_key"),
        ("AccountKey=abcdefghijklmnop", "storage_secret"),
        ("SharedAccessSignature=sv=2026&sig=secret", "storage_secret"),
        ("?sv=2026-01-01&sig=secret", "sas_parameter"),
        (
            "https://private.blob.core.windows.net/container/blob",
            "blob_url",
        ),
        ("encrypted_content", "protected_label"),
        ("protected_data", "protected_label"),
        ("sbx_1234567890abcdef", "sandbox_id"),
    ],
)
def test_matcher_detects_bounded_sensitive_patterns(value: str, rule: str) -> None:
    summary = scan_recursive({"nested": [{"value": value}]}, PrivacyMatcher(_canaries()))

    assert summary.rule_counts[rule] >= 1


@pytest.mark.parametrize(
    "value",
    [
        "PROMPT-CANARY-ALPHA",
        "provider-exact-juliet",
        "call-exact-kilo",
        "sandbox-exact-lima",
        "CREDENTIAL-CANARY-HOTEL",
        "BLOB-SAS-CANARY-INDIA",
    ],
)
def test_matcher_detects_exact_canaries_and_ids(value: str) -> None:
    summary = scan_recursive({"payload": value}, PrivacyMatcher(_canaries()))

    assert summary.violations >= 1


def test_durable_allowlist_accepts_hashes_refs_counters_timestamps_and_enums() -> None:
    value = {
        "PartitionKey": "partition-1",
        "RowKey": "row-1",
        "RuntimeStatus": "Completed",
        "Timestamp": "2026-09-07T10:00:00Z",
        "hash": "a" * 64,
        "ContentRef": {"object_id": "sha256-" + ("b" * 64)},
        "event_id": 7,
    }

    summary = scan_recursive(value, PrivacyMatcher(_canaries()), durable_envelope=True)

    assert summary.violations == 0


def test_durable_unknown_free_form_field_fails_closed() -> None:
    summary = scan_recursive(
        {"PartitionKey": "partition", "UnexpectedText": "ordinary but unclassified"},
        PrivacyMatcher(_canaries()),
        durable_envelope=True,
    )

    assert summary.rule_counts["unexpected_free_form"] == 1


def test_table_scanner_filters_and_scans_all_durable_surfaces() -> None:
    batch = Batch(
        items=(
            TableSnapshot("HubInstances", ({"Input": "safe"},)),
            TableSnapshot("HubHistory", ({"Result": "safe"},)),
            TableSnapshot("HubEntities", ({"State": "safe"},)),
            TableSnapshot("Unrelated", ({"Input": "PROMPT-CANARY-ALPHA"},)),
        )
    )

    results = scan_tables(batch, PrivacyMatcher(_canaries()))

    assert [result.surface for result in results] == [
        "Durable/Instances",
        "Durable/History",
        "Durable/Entities",
    ]
    assert all(result.status is ScanStatus.PASS for result in results)


def test_live_table_adapter_uses_table_item_attribute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import azure.data.tables

    class FakeCredential:
        pass

    class FakeTableItem:
        name = "HubHistory"

    class FakeTableClient:
        def list_entities(self) -> list[dict[str, str]]:
            return [{"PartitionKey": "partition", "Result": "safe"}]

    class FakeTableServiceClient:
        def __init__(self, *, endpoint: str, credential: object) -> None:
            assert endpoint.endswith(".table.core.windows.net")
            assert isinstance(credential, FakeCredential)

        def list_tables(self) -> list[FakeTableItem]:
            return [FakeTableItem()]

        def get_table_client(self, name: str) -> FakeTableClient:
            assert name == "HubHistory"
            return FakeTableClient()

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        azure.data.tables,
        "TableServiceClient",
        FakeTableServiceClient,
    )
    adapter = object.__new__(privacy.AzureLiveAdapter)
    adapter._resources = privacy.ResourceConfig(subscription_id="subscription")
    adapter._credential = FakeCredential()

    batch = adapter.tables()

    assert batch == Batch(
        items=(
            TableSnapshot(
                name="HubHistory",
                entities=({"PartitionKey": "partition", "Result": "safe"},),
            ),
        )
    )


def test_content_blob_allows_expected_canary_only_in_expected_class() -> None:
    body = b"PROMPT-CANARY-ALPHA"
    digest = hashlib.sha256(body).hexdigest()
    blob = BlobSnapshot(
        container="durable-loop-content",
        name=digest,
        metadata={
            "object_class": "prompt",
            "sha256": digest,
            "byte_count": str(len(body)),
        },
        tags={},
        headers={"content_length": len(body)},
        body=body,
    )

    result = scan_content_blobs(
        Batch((blob,)),
        PrivacyMatcher(_canaries()),
        content_container="durable-loop-content",
    )

    assert result.status is ScanStatus.PASS
    assert result.violations == 0


def test_content_blob_rejects_wrong_class_and_integrity_mismatch() -> None:
    body = b"PROMPT-CANARY-ALPHA"
    blob = BlobSnapshot(
        container="durable-loop-content",
        name="a" * 64,
        metadata={
            "object_class": "human",
            "sha256": "b" * 64,
            "byte_count": "999",
        },
        tags={},
        headers={"content_length": 998},
        body=body,
    )

    result = scan_content_blobs(
        Batch((blob,)),
        PrivacyMatcher(_canaries()),
        content_container="durable-loop-content",
    )

    assert result.status is ScanStatus.FAIL
    assert result.rule_counts["exact_prompt"] == 1
    assert result.rule_counts["content_name_hash_mismatch"] == 1
    assert result.rule_counts["content_sha256_mismatch"] == 1
    assert result.rule_counts["content_byte_count_mismatch"] == 1
    assert result.rule_counts["content_length_mismatch"] == 1


@pytest.mark.parametrize(
    "body",
    [
        b"CREDENTIAL-CANARY-HOTEL",
        b"https://private.blob.core.windows.net/container/blob?sig=secret",
    ],
)
def test_content_blob_forbids_credentials_and_blob_urls_everywhere(body: bytes) -> None:
    digest = hashlib.sha256(body).hexdigest()
    blob = BlobSnapshot(
        container="durable-loop-content",
        name=digest,
        metadata={
            "object_class": "prompt",
            "sha256": digest,
            "byte_count": str(len(body)),
        },
        tags={},
        headers={"content_length": len(body)},
        body=body,
    )

    result = scan_content_blobs(
        Batch((blob,)),
        PrivacyMatcher(_canaries()),
        content_container="durable-loop-content",
    )

    assert result.status is ScanStatus.FAIL


def test_queue_scanner_is_inconclusive_without_destructive_dequeue() -> None:
    result = scan_queues(
        Batch((QueueSnapshot("hub-control-00", {}, 2),)),
        PrivacyMatcher(_canaries()),
    )

    assert result.status is ScanStatus.INCONCLUSIVE
    assert result.code == "queue_not_drained"
    assert result.rule_counts == {"nonempty_queue": 1}


def test_filtering_excludes_secrets_and_unrelated_storage() -> None:
    assert classify_taskhub_table("HubHistory") == "Durable/History"
    assert classify_taskhub_table("OtherTable") is None
    assert is_taskhub_container("hub-largemessages", "durable-loop-content")
    assert not is_taskhub_container("azure-webjobs-secrets", "durable-loop-content")
    assert not is_taskhub_container("durable-loop-content", "durable-loop-content")
    assert is_taskhub_queue("hub-control-01")
    assert is_taskhub_queue("hub-workitems")
    assert not is_taskhub_queue("ordinary-queue")


def test_large_message_blob_fails_closed_on_unknown_free_form_field() -> None:
    result = scan_taskhub_blobs(
        Batch(
            (
                BlobSnapshot(
                    container="hub-largemessages",
                    name="envelope.json",
                    metadata={},
                    tags={},
                    headers={},
                    body=b'{"UnexpectedText":"ordinary but unclassified"}',
                ),
            )
        ),
        PrivacyMatcher(_canaries()),
        content_container="durable-loop-content",
    )

    assert result.status is ScanStatus.FAIL
    assert result.rule_counts["unexpected_free_form"] == 1


def test_large_message_blob_fails_closed_on_non_json_body() -> None:
    result = scan_taskhub_blobs(
        Batch(
            (
                BlobSnapshot(
                    container="hub-largemessages",
                    name="envelope.bin",
                    metadata={},
                    tags={},
                    headers={},
                    body=b"ordinary unstructured body",
                ),
            )
        ),
        PrivacyMatcher(_canaries()),
        content_container="durable-loop-content",
    )

    assert result.status is ScanStatus.FAIL
    assert result.rule_counts["durable_body_invalid_json"] == 1


def test_kusto_query_returns_only_aggregate_columns() -> None:
    query = build_kusto_aggregate_query(
        tables=("requests", "dependencies"),
        timestamp_column="timestamp",
        matcher=PrivacyMatcher(_canaries()),
        start=datetime(2026, 9, 7, 9, tzinfo=UTC),
        end=datetime(2026, 9, 7, 10, tzinfo=UTC),
    )

    assert "pack_all()" in query
    assert "| summarize scanned=count(), violations=countif(" in query
    assert "project " not in query
    assert "take " not in query
    assert "PROMPT-CANARY-ALPHA" in query
    assert query.strip().splitlines()[-1].startswith("| summarize ")


def test_server_aggregate_missing_data_is_inconclusive() -> None:
    result = privacy.scan_aggregate_observation(
        "Telemetry/AppInsights",
        AggregateObservation(scanned=0, violations=0),
    )

    assert result.status is ScanStatus.INCONCLUSIVE
    assert result.code == "expected_data_missing"


def test_apim_diagnostics_accept_zero_capture_and_control_override() -> None:
    diagnostics = {
        "durable-agent-loop-model": [_common_diagnostic()],
        "durable-agent-loop-model-control": [_control_diagnostic()],
        "durable-agent-loop-mcp": [_common_diagnostic()],
    }

    result = scan_apim_diagnostics(diagnostics, tuple(diagnostics))

    assert result.status is ScanStatus.PASS


def test_apim_diagnostics_reject_body_sensitive_header_and_control_sampling() -> None:
    unsafe = _common_diagnostic()
    unsafe["properties"]["frontend"]["request"] = {
        "body": {"bytes": 512},
        "headers": ["authorization"],
    }
    control = _control_diagnostic()
    control["properties"]["sampling"]["percentage"] = 100

    result = scan_apim_diagnostics(
        {
            "durable-agent-loop-model": [unsafe],
            "durable-agent-loop-model-control": [control],
            "durable-agent-loop-mcp": [_common_diagnostic()],
        },
        (
            "durable-agent-loop-model",
            "durable-agent-loop-model-control",
            "durable-agent-loop-mcp",
        ),
    )

    assert result.status is ScanStatus.FAIL
    assert result.rule_counts["body_capture_enabled"] == 1
    assert result.rule_counts["sensitive_header_capture"] == 1
    assert result.rule_counts["model_control_sampling_nonzero"] == 1


def test_apim_diagnostics_supports_strict_control_api_override() -> None:
    diagnostics = {
        "private-model": [_common_diagnostic()],
        "private-control": [_control_diagnostic()],
        "private-mcp": [_common_diagnostic()],
    }

    result = scan_apim_diagnostics(
        diagnostics,
        tuple(diagnostics),
        model_control_api="private-control",
    )

    assert result.status is ScanStatus.PASS


class _SandboxSequenceAdapter:
    def __init__(self, inventories: list[list[SandboxSnapshot]]) -> None:
        self._inventories = inventories
        self.calls = 0

    async def sandboxes(self, labels: dict[str, str]) -> list[SandboxSnapshot]:
        assert labels == {"app_hash": "a1-app"}
        index = min(self.calls, len(self._inventories) - 1)
        self.calls += 1
        return self._inventories[index]


async def _no_wait(_: float) -> None:
    return None


def test_sandbox_inventory_waits_for_final_zero_without_emitting_ids() -> None:
    owned = SandboxSnapshot(
        sandbox_id="ordinary-sandbox-id",
        labels={"app_hash": "a1-app"},
    )
    adapter = _SandboxSequenceAdapter([[owned], []])

    result = asyncio.run(
        scan_sandbox_inventory(
            adapter,  # type: ignore[arg-type]
            PrivacyMatcher(_canaries()),
            selector={"app_hash": "a1-app"},
            wait_seconds=1,
            poll_seconds=1,
            sleep=_no_wait,
        )
    )

    assert result.status is ScanStatus.PASS
    assert adapter.calls == 2
    assert "ordinary-sandbox-id" not in render_result(result)


def test_sandbox_inventory_fails_when_owned_inventory_remains() -> None:
    owned = SandboxSnapshot(
        sandbox_id="ordinary-sandbox-id",
        labels={"app_hash": "a1-app"},
    )
    adapter = _SandboxSequenceAdapter([[owned]])

    result = asyncio.run(
        scan_sandbox_inventory(
            adapter,  # type: ignore[arg-type]
            PrivacyMatcher(_canaries()),
            selector={"app_hash": "a1-app"},
            wait_seconds=0,
            poll_seconds=1,
        )
    )

    assert result.status is ScanStatus.FAIL
    assert result.rule_counts == {"remaining_owned_sandbox": 1}


def test_exception_projection_never_includes_message_or_url() -> None:
    secret = "https://private.example.test/?sig=credential"
    result = inconclusive_from_exception(
        "Durable/History",
        RuntimeError(secret),
    )
    rendered = render_result(result)

    assert result.code == "access_RuntimeError"
    assert secret not in rendered
    assert "credential" not in rendered


def test_renderer_contains_only_fixed_aggregate_fields() -> None:
    result = ScanResult(
        surface="Durable/History",
        scanned=41,
        violations=2,
        status=ScanStatus.FAIL,
        rule_counts={"exact_prompt": 2},
    )

    assert json.loads(render_result(result)) == {
        "rule_counts": {"exact_prompt": 2},
        "scanned": 41,
        "status": "FAIL",
        "surface": "Durable/History",
        "violations": 2,
    }


def test_aggregate_exit_codes_are_deterministic_and_optional_missing_is_safe() -> None:
    passed = ScanAggregate(
        (
            ScanResult("Durable/History", 1, 0, ScanStatus.PASS),
            ScanResult("Deployment/Activity", 0, 0, ScanStatus.NOT_APPLICABLE),
        )
    )
    inconclusive = ScanAggregate((ScanResult("Durable/History", 0, 0, ScanStatus.INCONCLUSIVE),))
    failed = ScanAggregate(
        (
            ScanResult("Durable/History", 0, 0, ScanStatus.INCONCLUSIVE),
            ScanResult("Content/ProtectedBlobs", 1, 1, ScanStatus.FAIL),
        )
    )

    assert (passed.status, passed.exit_code) == (ScanStatus.PASS, 0)
    assert (inconclusive.status, inconclusive.exit_code) == (
        ScanStatus.INCONCLUSIVE,
        2,
    )
    assert (failed.status, failed.exit_code) == (ScanStatus.FAIL, 1)
    assert json.loads(render_aggregate(passed))["status"] == "PASS"


def test_full_fixture_scan_passes_and_never_renders_canaries() -> None:
    operator = _operator()
    aggregate = asyncio.run(
        run_scan(
            FixtureLiveAdapter(operator.fixtures or {}),
            operator,
            _timing(),
        )
    )
    rendered = "\n".join(
        [*(render_result(result) for result in aggregate.results), render_aggregate(aggregate)]
    )

    assert aggregate.status is ScanStatus.PASS
    assert aggregate.exit_code == 0
    for values in _canary_mapping().values():
        assert values[0] not in rendered
    for exact_id in _canaries().exact_ids:
        assert exact_id not in rendered
    assert all(json.loads(line) for line in rendered.splitlines())


def test_run_scan_redacts_adapter_exception_messages() -> None:
    class FailingAdapter(FixtureLiveAdapter):
        def tables(self) -> Batch:
            raise RuntimeError("CREDENTIAL-CANARY-HOTEL?sig=secret")

    operator = _operator()
    aggregate = asyncio.run(
        run_scan(
            FailingAdapter(operator.fixtures or {}),
            operator,
            _timing(),
        )
    )
    rendered = "\n".join(render_result(result) for result in aggregate.results)

    assert "CREDENTIAL-CANARY-HOTEL" not in rendered
    assert "sig=secret" not in rendered
    table_results = [
        result for result in aggregate.results if result.surface.startswith("Durable/")
    ]
    assert all(result.status is ScanStatus.INCONCLUSIVE for result in table_results)


def test_main_scan_json_pass_fail_and_inconclusive_exit_codes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    healthy = json.dumps(_operator_payload(_healthy_fixtures())).encode()
    assert (
        main(
            [
                "scan-json",
                "--telemetry-lag-retries",
                "0",
                "--sandbox-wait-seconds",
                "0",
            ],
            io.BytesIO(healthy),
        )
        == 0
    )
    pass_output = capsys.readouterr().out
    assert json.loads(pass_output.splitlines()[-1])["status"] == "PASS"

    failed_fixtures = _healthy_fixtures()
    failed_fixtures["tables"][1]["entities"][0]["Result"] = "PROMPT-CANARY-ALPHA"
    failed = json.dumps(_operator_payload(failed_fixtures)).encode()
    assert (
        main(
            [
                "scan-json",
                "--telemetry-lag-retries",
                "0",
                "--sandbox-wait-seconds",
                "0",
            ],
            io.BytesIO(failed),
        )
        == 1
    )
    fail_output = capsys.readouterr().out
    assert "PROMPT-CANARY-ALPHA" not in fail_output
    assert json.loads(fail_output.splitlines()[-1])["status"] == "FAIL"

    inconclusive_fixtures = _healthy_fixtures()
    inconclusive_fixtures["app_insights"] = {"scanned": 0, "violations": 0}
    inconclusive = json.dumps(_operator_payload(inconclusive_fixtures)).encode()
    assert (
        main(
            [
                "scan-json",
                "--telemetry-lag-retries",
                "0",
                "--sandbox-wait-seconds",
                "0",
            ],
            io.BytesIO(inconclusive),
        )
        == 2
    )
    inconclusive_output = capsys.readouterr().out
    assert json.loads(inconclusive_output.splitlines()[-1])["status"] == "INCONCLUSIVE"


def test_main_duplicate_key_failure_is_json_only_and_redacted(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(
        ["scan-json"],
        io.BytesIO(b'{"canaries":{"secret":"first","secret":"second"}}'),
    )
    output = capsys.readouterr()

    assert exit_code == 2
    assert output.err == ""
    assert "first" not in output.out
    assert "second" not in output.out
    lines = output.out.splitlines()
    assert json.loads(lines[0])["code"] == "input_DuplicateKeyError"
    assert json.loads(lines[-1])["status"] == "INCONCLUSIVE"


def test_scan_json_does_not_construct_live_azure_adapter(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        privacy,
        "AzureLiveAdapter",
        lambda *_args, **_kwargs: pytest.fail("live adapter must not be constructed"),
    )

    exit_code = main(
        [
            "scan-json",
            "--telemetry-lag-retries",
            "0",
            "--sandbox-wait-seconds",
            "0",
        ],
        io.BytesIO(json.dumps(_operator_payload(_healthy_fixtures())).encode()),
    )

    assert exit_code == 0
    capsys.readouterr()


def test_source_uses_default_credential_and_avoids_secret_retrieval_apis() -> None:
    source = Path("eng/scripts/durable_loop_privacy_qualification.py").read_text(encoding="utf-8")

    assert "DefaultAzureCredential" in source
    assert "listPublishingCredentials" not in source
    assert "publishingProfiles" not in source
    assert "listKeys" not in source
    assert "appsettings/list" not in source
    assert "connection_string" not in source
    assert "account_key=" not in source


def test_match_summary_reports_total_without_values() -> None:
    summary = MatchSummary(
        scanned=5,
        rule_counts={"exact_prompt": 1, "blob_url": 2},
    )

    assert summary.violations == 3
