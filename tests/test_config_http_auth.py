from __future__ import annotations

import pytest

from azure_functions_agents.config.http_auth import (
    resolve_aca_submission_auth,
    resolve_http_trigger_auth,
)
from azure_functions_agents.config.schema import EndpointAuthConfig, EntraAuthConfig


def test_http_trigger_auth_keeps_deprecated_flat_levels_compatible() -> None:
    assert resolve_http_trigger_auth({"auth_level": "ADMIN"}).mode == "admin"


@pytest.mark.parametrize(
    "trigger_args",
    [
        {"http_auth": {"mode": 1}},
        {"http_auth": {"mode": "function", "entra": {"allowed_audiences": "api://app"}}},
        {"auth_level": 1},
    ],
)
def test_http_trigger_auth_rejects_coercion_at_the_trust_boundary(
    trigger_args: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        resolve_http_trigger_auth(trigger_args)


def test_aca_submission_auth_requires_exact_entra_policy_parity() -> None:
    builtin = EndpointAuthConfig(
        mode="entra",
        entra=EntraAuthConfig(
            allowed_audiences=["api://one"],
            allowed_client_ids=["client-one"],
        ),
    )

    with pytest.raises(ValueError, match="identical resolved auth policies"):
        resolve_aca_submission_auth(
            builtin_auth=builtin,
            trigger_args={
                "http_auth": {
                    "mode": "entra",
                    "entra": {
                        "allowed_audiences": ["api://two"],
                        "allowed_client_ids": ["client-one"],
                    },
                }
            },
        )


def test_aca_submission_auth_accepts_matching_and_single_surface_policies() -> None:
    builtin = EndpointAuthConfig(
        mode="entra",
        entra=EntraAuthConfig(allowed_audiences=["api://one"]),
    )
    matching_args = {
        "http_auth": {"mode": "entra", "entra": {"allowed_audiences": ["api://one"]}}
    }

    assert (
        resolve_aca_submission_auth(
            builtin_auth=builtin,
            trigger_args=matching_args,
        )
        == builtin
    )
    assert (
        resolve_aca_submission_auth(
            builtin_auth=None,
            trigger_args={"http_auth": "admin"},
        ).mode
        == "admin"
    )
