from __future__ import annotations

import pytest

from azure_functions_agents.service_bus_delivery_identity import (
    ServiceBusDeliveryIdentity,
    ServiceBusDeliveryIdentityError,
)


def _identity(
    environment: dict[str, str],
    *,
    connection_name: str = "ServiceBus",
    queue_name: str = "jobs",
    sequence_number: int = 42,
) -> ServiceBusDeliveryIdentity:
    return ServiceBusDeliveryIdentity.create(
        environment=environment,
        connection_name=connection_name,
        queue_name=queue_name,
        sequence_number=sequence_number,
        app_hash="a1-app",
        agent_slug="worker",
    )


def test_identity_namespace_precedes_connection_string() -> None:
    identity = _identity(
        {
            "ServiceBus__fullyQualifiedNamespace": "Example.ServiceBus.Windows.Net.",
            "ServiceBus": (
                "Endpoint=sb://ignored.servicebus.windows.net/;"
                "SharedAccessKeyName=Root;SharedAccessKey=hunter2"
            ),
        }
    )
    equivalent = _identity(
        {"ServiceBus__fullyQualifiedNamespace": "example.servicebus.windows.net"}
    )

    assert identity == equivalent
    assert identity.entity_fingerprint.startswith("sb1-")
    assert identity.idempotency_key.startswith("sbd1-")


def test_identity_parses_only_connection_string_endpoint() -> None:
    secret = "hunter2"
    identity = _identity(
        {
            "ServiceBus": (
                "SharedAccessKeyName=Root;"
                f"SharedAccessKey={secret};"
                "Endpoint=sb://namespace.servicebus.windows.net/"
            )
        }
    )

    assert identity == _identity(
        {"ServiceBus__fullyQualifiedNamespace": "namespace.servicebus.windows.net"}
    )
    assert secret not in repr(identity)


@pytest.mark.parametrize(
    ("environment", "connection_name", "queue_name", "sequence_number"),
    [
        ({}, "ServiceBus", "jobs", 1),
        ({"ServiceBus": "SharedAccessKey=hunter2"}, "ServiceBus", "jobs", 1),
        (
            {"ServiceBus__fullyQualifiedNamespace": "sb://user@namespace.test"},
            "ServiceBus",
            "jobs",
            1,
        ),
        ({"ServiceBus__fullyQualifiedNamespace": "namespace.test"}, "", "jobs", 1),
        ({"ServiceBus__fullyQualifiedNamespace": "namespace.test"}, "ServiceBus", "", 1),
        ({"ServiceBus__fullyQualifiedNamespace": "namespace.test"}, "ServiceBus", "jobs", -1),
        (
            {"ServiceBus__fullyQualifiedNamespace": "namespace.test"},
            "ServiceBus",
            "jobs",
            2**64,
        ),
    ],
)
def test_identity_rejects_unstable_or_invalid_inputs_without_secret_leak(
    environment: dict[str, str],
    connection_name: str,
    queue_name: str,
    sequence_number: int,
) -> None:
    with pytest.raises(ServiceBusDeliveryIdentityError) as exc_info:
        _identity(
            environment,
            connection_name=connection_name,
            queue_name=queue_name,
            sequence_number=sequence_number,
        )

    assert "hunter2" not in str(exc_info.value)


def test_identity_changes_across_entity_delivery_app_and_agent_boundaries() -> None:
    environment = {"ServiceBus__fullyQualifiedNamespace": "namespace.test"}
    baseline = _identity(environment)
    other_queue = _identity(environment, queue_name="other")
    other_namespace = _identity(
        {"ServiceBus__fullyQualifiedNamespace": "other.namespace.test"}
    )
    other_sequence = _identity(environment, sequence_number=43)
    other_app = ServiceBusDeliveryIdentity.create(
        environment=environment,
        connection_name="ServiceBus",
        queue_name="jobs",
        sequence_number=42,
        app_hash="a1-other",
        agent_slug="worker",
    )
    other_agent = ServiceBusDeliveryIdentity.create(
        environment=environment,
        connection_name="ServiceBus",
        queue_name="jobs",
        sequence_number=42,
        app_hash="a1-app",
        agent_slug="other",
    )

    assert baseline.entity_fingerprint != other_queue.entity_fingerprint
    assert baseline.entity_fingerprint != other_namespace.entity_fingerprint
    assert baseline.entity_fingerprint == other_sequence.entity_fingerprint
    assert len(
        {
            baseline.idempotency_key,
            other_queue.idempotency_key,
            other_namespace.idempotency_key,
            other_sequence.idempotency_key,
            other_app.idempotency_key,
            other_agent.idempotency_key,
        }
    ) == 6
