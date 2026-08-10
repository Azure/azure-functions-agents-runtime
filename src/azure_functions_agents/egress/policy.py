"""Compile derived HTTP destinations into a fail-closed egress policy."""

from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import SplitResult, urlsplit

from ..transport.transport_models import (
    SandboxEgressHeader,
    SandboxEgressHostRule,
    SandboxEgressPolicy,
    SandboxEgressRule,
    SandboxEgressRuleAction,
    SandboxEgressRuleMatch,
    SandboxProvisioningError,
)

CONTROL_PLANE_DENY_HOSTS = (
    "management.azure.com",
    "management.azuredevcompute.io",
)
# Keep within the service's default policy limit without relying on optional quota overrides.
MAX_EGRESS_POLICY_RULES = 500


def derive_destination_hosts(
    *,
    web_request_allowed_hosts: Iterable[str] | None,
    mcp_urls: Iterable[str] = (),
    model_endpoint: str | None = None,
    telemetry_endpoint: str | None = None,
) -> tuple[str, ...]:
    """Return the host union required by the reachable sandbox workload."""

    hosts: set[str] = set()
    if web_request_allowed_hosts is None:
        # Preserve the existing tool contract: an omitted allowlist permits public hosts.
        hosts.add("*")
    else:
        hosts.update(_normalize_host(host) for host in web_request_allowed_hosts)
    for url in mcp_urls:
        hosts.add(_host_from_url(url, "MCP URL"))
    if model_endpoint:
        hosts.add(_host_from_url(model_endpoint, "model endpoint"))
    if telemetry_endpoint:
        hosts.add(_host_from_url(telemetry_endpoint, "telemetry endpoint"))
    return tuple(sorted(hosts))


def compile_egress_policy(
    *,
    web_request_allowed_hosts: Iterable[str] | None,
    mcp_urls: Iterable[str] = (),
    model_endpoint: str | None = None,
    telemetry_endpoint: str | None = None,
    rules: Iterable[SandboxEgressRule] = (),
    model_headers: Iterable[SandboxEgressHeader] = (),
) -> SandboxEgressPolicy:
    """Build an explicit Full-inspection policy from runtime-derived destinations."""

    supplied_rules = tuple(rules)
    resolved_model_headers = tuple(model_headers)
    if resolved_model_headers:
        if not model_endpoint:
            raise SandboxProvisioningError(
                "Model egress headers require a configured model endpoint."
            )
        supplied_rules = (
            *supplied_rules,
            build_header_transform_rule(
                name="model-auth",
                url=model_endpoint,
                headers=resolved_model_headers,
            ),
        )
    _validate_typed_rules(supplied_rules)
    ordered_rules = _order_rules(supplied_rules)
    validate_egress_rule_order(ordered_rules)
    destinations = derive_destination_hosts(
        web_request_allowed_hosts=web_request_allowed_hosts,
        mcp_urls=mcp_urls,
        model_endpoint=model_endpoint,
        telemetry_endpoint=telemetry_endpoint,
    )
    host_rules = tuple(
        SandboxEgressHostRule.create(host=host, action="Deny")
        for host in CONTROL_PLANE_DENY_HOSTS
    ) + tuple(
        SandboxEgressHostRule.create(host=host, action="Allow")
        for host in destinations
        if host not in CONTROL_PLANE_DENY_HOSTS
    )
    _validate_rule_count(host_rules, ordered_rules)
    return SandboxEgressPolicy.create(
        default_action="Deny",
        traffic_inspection="Full",
        host_rules=host_rules,
        rules=ordered_rules,
    )


def build_header_transform_rule(
    *,
    name: str,
    url: str,
    headers: Iterable[SandboxEgressHeader],
) -> SandboxEgressRule:
    """Build the narrow transformation that injects outbound headers for one URL."""

    parsed = _parse_http_url(url, "egress header URL")
    header_values = tuple(headers)
    if not header_values:
        raise SandboxProvisioningError("Egress header rules require at least one header.")
    match = SandboxEgressRuleMatch.create(
        host=parsed.hostname or "",
        path=parsed.path or "/",
    )
    action = SandboxEgressRuleAction.create(type="Transform", headers=header_values)
    return SandboxEgressRule.create(name=name, match=match, action=action)


def validate_egress_rule_order(rules: Iterable[SandboxEgressRule]) -> None:
    """Reject an Allow rule that would hide a later, narrower Deny rule."""

    ordered = tuple(rules)
    for index, earlier in enumerate(ordered):
        if earlier.action.type != "Allow":
            continue
        for later in ordered[index + 1 :]:
            if later.action.type != "Deny":
                continue
            if _match_covers(earlier.match, later.match):
                raise SandboxProvisioningError(
                    "A broad egress Allow rule must not shadow a narrower Deny rule."
                )


def _validate_typed_rules(rules: tuple[SandboxEgressRule, ...]) -> None:
    if not all(isinstance(rule, SandboxEgressRule) for rule in rules):
        raise SandboxProvisioningError("Sandbox egress rules must be typed values.")


def _validate_rule_count(
    host_rules: tuple[SandboxEgressHostRule, ...],
    rules: tuple[SandboxEgressRule, ...],
) -> None:
    if len(host_rules) + len(rules) > MAX_EGRESS_POLICY_RULES:
        raise SandboxProvisioningError(
            "Sandbox egress policy exceeds the supported rule limit."
        )


def _order_rules(rules: tuple[SandboxEgressRule, ...]) -> tuple[SandboxEgressRule, ...]:
    return tuple(
        sorted(
            rules,
            key=lambda rule: (
                -_match_specificity(rule.match),
                _action_priority(rule.action.type),
            ),
        )
    )


def _action_priority(action: str) -> int:
    if action == "Deny":
        return 0
    if action in {"Transform", "Rewrite"}:
        return 1
    return 2


def _match_specificity(match: SandboxEgressRuleMatch) -> int:
    host_score = 0 if _is_wildcard_host(match.host) else 10
    path_score = 0 if match.path is None else len(match.path.split("/"))
    method_score = len(match.methods)
    return host_score + path_score + method_score


def _match_covers(
    broader: SandboxEgressRuleMatch,
    narrower: SandboxEgressRuleMatch,
) -> bool:
    return (
        _host_covers(broader.host, narrower.host)
        and _methods_cover(broader.methods, narrower.methods)
        and _path_covers(broader.path, narrower.path)
    )


def _host_covers(broader: str, narrower: str) -> bool:
    if broader == "*":
        return True
    if broader == narrower:
        return True
    return broader.startswith("*.") and narrower.endswith(broader[1:])


def _methods_cover(broader: tuple[str, ...], narrower: tuple[str, ...]) -> bool:
    return not broader or set(narrower).issubset(broader)


def _path_covers(broader: str | None, narrower: str | None) -> bool:
    if broader is None:
        return True
    if narrower is None:
        return False
    if broader == "/":
        return True
    return narrower == broader or narrower.startswith(f"{broader.rstrip('/')}/")


def _host_from_url(value: str, field_name: str) -> str:
    return _parse_http_url(value, field_name).hostname or ""


def _parse_http_url(value: str, field_name: str) -> SplitResult:
    if not isinstance(value, str):
        raise SandboxProvisioningError(f"{field_name} must be an HTTP URL.")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise SandboxProvisioningError(f"{field_name} must be an HTTP URL.")
    return parsed


def _normalize_host(value: str) -> str:
    if not isinstance(value, str):
        raise SandboxProvisioningError("Egress hosts must be strings.")
    host = value.strip().casefold()
    if not host or "/" in host or "://" in host or host.startswith("."):
        raise SandboxProvisioningError("Egress hosts have an invalid shape.")
    return host


def _is_wildcard_host(host: str) -> bool:
    return host == "*" or host.startswith("*.")
