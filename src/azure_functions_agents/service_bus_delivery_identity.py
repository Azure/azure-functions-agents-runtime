"""Stable, non-secret delivery identities for Service Bus queue triggers."""

from __future__ import annotations

import hashlib
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlparse

from .session_state._label_encoding import encode_label_safe_digest
from .session_state.identity import frame_canonical_components

SERVICE_BUS_ENTITY_FINGERPRINT_VERSION = "sb1"
SERVICE_BUS_DELIVERY_KEY_VERSION = "sbd1"
_MAX_SEQUENCE_NUMBER = 2**64 - 1


class ServiceBusDeliveryIdentityError(ValueError):
    """Raised when stable Service Bus delivery identity cannot be derived."""


@dataclass(frozen=True, slots=True)
class ServiceBusDeliveryIdentity:
    """Owner-neutral Service Bus entity and delivery fingerprints."""

    entity_fingerprint: str
    idempotency_key: str
    sequence_number: int

    @classmethod
    def create(
        cls,
        *,
        environment: Mapping[str, str],
        connection_name: str,
        queue_name: str,
        sequence_number: int,
        app_hash: str,
        agent_slug: str,
    ) -> ServiceBusDeliveryIdentity:
        connection = _required_component(connection_name, "connection name")
        queue = _required_component(queue_name, "queue name")
        app = _required_component(app_hash, "application identity")
        agent = _required_component(agent_slug, "agent slug")
        sequence = _validate_sequence_number(sequence_number)
        namespace = _resolve_namespace(environment, connection)
        entity_fingerprint = _digest_label(
            SERVICE_BUS_ENTITY_FINGERPRINT_VERSION,
            ("service_bus_entity", SERVICE_BUS_ENTITY_FINGERPRINT_VERSION, namespace, queue),
        )
        idempotency_key = _digest_label(
            SERVICE_BUS_DELIVERY_KEY_VERSION,
            (
                "service_bus_delivery",
                SERVICE_BUS_DELIVERY_KEY_VERSION,
                app,
                agent,
                entity_fingerprint,
                str(sequence),
            ),
        )
        return cls(
            entity_fingerprint=entity_fingerprint,
            idempotency_key=idempotency_key,
            sequence_number=sequence,
        )


def _required_component(value: str, label: str) -> str:
    normalized = unicodedata.normalize("NFC", value.strip())
    if not normalized:
        raise ServiceBusDeliveryIdentityError(f"Service Bus {label} is required.")
    return normalized


def _validate_sequence_number(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ServiceBusDeliveryIdentityError(
            "Service Bus sequence_number must be an unsigned 64-bit integer."
        )
    if value < 0 or value > _MAX_SEQUENCE_NUMBER:
        raise ServiceBusDeliveryIdentityError(
            "Service Bus sequence_number must be an unsigned 64-bit integer."
        )
    return value


def _resolve_namespace(environment: Mapping[str, str], connection_name: str) -> str:
    identity_value = environment.get(f"{connection_name}__fullyQualifiedNamespace")
    if identity_value is not None and identity_value.strip():
        return _normalize_namespace(identity_value)

    connection_string = environment.get(connection_name)
    if connection_string is None or not connection_string.strip():
        raise ServiceBusDeliveryIdentityError(
            "Service Bus namespace configuration is unavailable."
        )
    endpoint = _connection_string_endpoint(connection_string)
    return _normalize_namespace(endpoint)


def _connection_string_endpoint(connection_string: str) -> str:
    endpoint: str | None = None
    for component in connection_string.split(";"):
        key, separator, value = component.partition("=")
        if not separator or key.strip().casefold() != "endpoint":
            continue
        if endpoint is not None:
            raise ServiceBusDeliveryIdentityError(
                "Service Bus namespace configuration is invalid."
            )
        endpoint = value.strip()
    if not endpoint:
        raise ServiceBusDeliveryIdentityError(
            "Service Bus namespace configuration is invalid."
        )
    return endpoint


def _normalize_namespace(value: str) -> str:
    candidate = value.strip()
    parsed = urlparse(candidate if "://" in candidate else f"sb://{candidate}")
    if (
        parsed.scheme.casefold() not in {"sb", "https"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ServiceBusDeliveryIdentityError(
            "Service Bus namespace configuration is invalid."
        )
    hostname = parsed.hostname
    if hostname is None:
        raise ServiceBusDeliveryIdentityError(
            "Service Bus namespace configuration is invalid."
        )
    try:
        normalized = hostname.rstrip(".").encode("idna").decode("ascii").casefold()
    except UnicodeError as exc:
        raise ServiceBusDeliveryIdentityError(
            "Service Bus namespace configuration is invalid."
        ) from exc
    if not normalized:
        raise ServiceBusDeliveryIdentityError(
            "Service Bus namespace configuration is invalid."
        )
    return normalized


def _digest_label(version: str, components: tuple[str, ...]) -> str:
    digest = hashlib.sha256(frame_canonical_components(components)).digest()
    return f"{version}-{encode_label_safe_digest(digest)}"
