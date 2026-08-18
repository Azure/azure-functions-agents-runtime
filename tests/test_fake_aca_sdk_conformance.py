"""Runtime conformance guard for the ACA SDK fake used by transport tests.

The preview package remains optional in the normal test environment, so this
module imports it dynamically.  When the pinned extra is installed, the
declarative inventory below compares every SDK-shaped fake member to its
runtime type information without issuing a network request.
"""

from __future__ import annotations

import inspect
import types
from collections.abc import AsyncIterator
from dataclasses import MISSING, Field, dataclass, fields
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
from types import ModuleType
from typing import Literal, Union, get_args, get_origin, get_type_hints

import pytest
from azure.core.polling import AsyncLROPoller

from tests.doubles.fake_aca_sdk import (
    FakePoller,
    FakeSdkAddPortRequest,
    FakeSdkAutoDeletePolicy,
    FakeSdkAutoSuspendPolicy,
    FakeSdkDirListing,
    FakeSdkEgressHeader,
    FakeSdkEgressHeaderValueRef,
    FakeSdkEgressHostRule,
    FakeSdkEgressManagedIdentityRef,
    FakeSdkEgressPolicy,
    FakeSdkEgressRule,
    FakeSdkEgressRuleAction,
    FakeSdkEgressRuleMatch,
    FakeSdkEgressSecretRef,
    FakeSdkEnvironment,
    FakeSdkExecResult,
    FakeSdkFileInfo,
    FakeSdkGroupClient,
    FakeSdkLifecyclePolicy,
    FakeSdkPortAuthConfig,
    FakeSdkPortAuthEntraId,
    FakeSdkPortIpAccessControl,
    FakeSdkPortIpAccessControlRule,
    FakeSdkSandboxClient,
    FakeSdkSandboxSummary,
    FakeSdkSandboxVolume,
    FakeSdkSnapshot,
    FakeSdkSnapshotGpu,
    FakeSdkSnapshotResources,
)

_SDK_MODULE = ".".join(("azure", "containerapps", "sandbox"))
_PINNED_VERSION = "0.1.0b4"


@dataclass(frozen=True)
class _ModelShape:
    fake: type[object]
    real_name: str
    fields: tuple[str, ...]


@dataclass(frozen=True)
class _OperationShape:
    fake_owner: type[object]
    real_owner: str
    method: str
    return_shape: Literal["value", "async_iterable", "poller"]
    item_type: str | None = None
    poller_result_type: str | None = None


_FAKE_TO_REAL = {
    "FakeSdkAutoDeletePolicy": "AutoDeletePolicy",
    "FakeSdkAutoSuspendPolicy": "AutoSuspendPolicy",
    "FakeSdkAddPortRequest": "AddPortRequest",
    "FakeSdkDirListing": "DirListing",
    "FakeSdkEgressHeader": "EgressHeader",
    "FakeSdkEgressHeaderValueRef": "EgressHeaderValueRef",
    "FakeSdkEgressHostRule": "EgressHostRule",
    "FakeSdkEgressManagedIdentityRef": "EgressManagedIdentityRef",
    "FakeSdkEgressPolicy": "EgressPolicy",
    "FakeSdkEgressRule": "EgressRule",
    "FakeSdkEgressRuleAction": "EgressRuleAction",
    "FakeSdkEgressRuleMatch": "EgressRuleMatch",
    "FakeSdkEgressSecretRef": "EgressSecretRef",
    "FakeSdkExecResult": "ExecResult",
    "FakeSdkFileInfo": "FileInfo",
    "FakeSdkLifecyclePolicy": "LifecyclePolicy",
    "FakeSdkPortAuthConfig": "PortAuthConfig",
    "FakeSdkPortAuthEntraId": "PortAuthEntraId",
    "FakeSdkPortIpAccessControl": "PortIpAccessControl",
    "FakeSdkPortIpAccessControlRule": "PortIpAccessControlRule",
    "FakeSdkSandboxClient": "SandboxClient",
    "FakeSdkSandboxSummary": "Sandbox",
    "FakeSdkSandboxVolume": "SandboxVolume",
    "FakeSdkSnapshot": "Snapshot",
    "FakeSdkSnapshotGpu": "SnapshotGpu",
    "FakeSdkSnapshotResources": "SnapshotResources",
}

# A fake can intentionally project a response subset, but every field it does
# expose is listed here and must retain the SDK's name, type, default, and
# constructor position.  Snapshot is complete because the reconciler retains
# its metadata; Sandbox is the inventory projection consumed by this adapter.
_MODEL_SHAPES = (
    _ModelShape(FakeSdkAutoSuspendPolicy, "AutoSuspendPolicy", ("enabled", "interval", "mode")),
    _ModelShape(FakeSdkAutoDeletePolicy, "AutoDeletePolicy", ("enabled", "delete_interval_seconds")),
    _ModelShape(FakeSdkLifecyclePolicy, "LifecyclePolicy", ("auto_suspend", "auto_delete")),
    _ModelShape(
        FakeSdkPortAuthEntraId,
        "PortAuthEntraId",
        ("enabled", "emails"),
    ),
    _ModelShape(FakeSdkPortAuthConfig, "PortAuthConfig", ("anonymous", "entra_id")),
    _ModelShape(
        FakeSdkPortIpAccessControlRule,
        "PortIpAccessControlRule",
        ("name", "action", "priority", "source_cidrs"),
    ),
    _ModelShape(
        FakeSdkPortIpAccessControl,
        "PortIpAccessControl",
        ("default_action", "rules"),
    ),
    _ModelShape(
        FakeSdkAddPortRequest,
        "AddPortRequest",
        ("port", "auth", "protocol", "activation_mode", "ip_access_control"),
    ),
    _ModelShape(FakeSdkSandboxVolume, "SandboxVolume", ("volume_name", "mountpoint", "read_only")),
    _ModelShape(FakeSdkFileInfo, "FileInfo", ("name", "path", "size", "is_directory", "modified_at", "mode")),
    _ModelShape(FakeSdkDirListing, "DirListing", ("path", "entries")),
    _ModelShape(FakeSdkExecResult, "ExecResult", ("exit_code", "stdout", "stderr")),
    _ModelShape(
        FakeSdkEgressPolicy,
        "EgressPolicy",
        ("default_action", "host_rules", "rules", "traffic_inspection"),
    ),
    _ModelShape(FakeSdkEgressHostRule, "EgressHostRule", ("pattern", "action")),
    _ModelShape(FakeSdkEgressRuleMatch, "EgressRuleMatch", ("host", "path", "methods")),
    _ModelShape(
        FakeSdkEgressManagedIdentityRef,
        "EgressManagedIdentityRef",
        ("identity_type", "resource", "identity_resource_id", "format"),
    ),
    _ModelShape(FakeSdkEgressSecretRef, "EgressSecretRef", ("secret_id", "secret_key", "format")),
    _ModelShape(
        FakeSdkEgressHeaderValueRef,
        "EgressHeaderValueRef",
        ("secret_ref", "managed_identity_ref"),
    ),
    _ModelShape(FakeSdkEgressHeader, "EgressHeader", ("operation", "name", "value", "value_ref")),
    _ModelShape(
        FakeSdkEgressRuleAction,
        "EgressRuleAction",
        ("type", "host", "path", "scheme", "headers"),
    ),
    _ModelShape(FakeSdkEgressRule, "EgressRule", ("name", "match", "action")),
    _ModelShape(
        FakeSdkSandboxSummary,
        "Sandbox",
        ("id", "state", "labels", "lifecycle", "created_at"),
    ),
    _ModelShape(
        FakeSdkSnapshotGpu,
        "SnapshotGpu",
        ("sku", "quantity"),
    ),
    _ModelShape(
        FakeSdkSnapshotResources,
        "SnapshotResources",
        ("cpu", "memory", "disk", "gpu"),
    ),
    _ModelShape(
        FakeSdkSnapshot,
        "Snapshot",
        ("id", "labels", "sandbox_id", "status", "vmm_type", "created_at_utc", "resources"),
    ),
)

_OPERATION_SHAPES = (
    _OperationShape(FakeSdkSandboxClient, "SandboxClient", "list_files", "value"),
    _OperationShape(FakeSdkSandboxClient, "SandboxClient", "stat_file", "value"),
    _OperationShape(FakeSdkSandboxClient, "SandboxClient", "read_file", "value"),
    _OperationShape(FakeSdkSandboxClient, "SandboxClient", "write_file", "value"),
    _OperationShape(FakeSdkSandboxClient, "SandboxClient", "delete_file", "value"),
    _OperationShape(FakeSdkSandboxClient, "SandboxClient", "mkdir", "value"),
    _OperationShape(FakeSdkSandboxClient, "SandboxClient", "exec", "value"),
    _OperationShape(FakeSdkSandboxClient, "SandboxClient", "get", "value"),
    _OperationShape(
        FakeSdkSandboxClient,
        "SandboxClient",
        "begin_stop",
        "poller",
        poller_result_type="None",
    ),
    _OperationShape(FakeSdkSandboxClient, "SandboxClient", "resume", "value"),
    _OperationShape(
        FakeSdkSandboxClient,
        "SandboxClient",
        "begin_delete",
        "poller",
        poller_result_type="None",
    ),
    _OperationShape(FakeSdkSandboxClient, "SandboxClient", "set_lifecycle_policy", "value"),
    _OperationShape(FakeSdkSandboxClient, "SandboxClient", "close", "value"),
    _OperationShape(
        FakeSdkGroupClient,
        "SandboxGroupClient",
        "begin_create_sandbox",
        "poller",
        poller_result_type="SandboxClient",
    ),
    _OperationShape(
        FakeSdkGroupClient,
        "SandboxGroupClient",
        "list_sandboxes",
        "async_iterable",
        item_type="Sandbox",
    ),
    _OperationShape(
        FakeSdkGroupClient,
        "SandboxGroupClient",
        "begin_delete_sandbox",
        "poller",
        poller_result_type="None",
    ),
    _OperationShape(
        FakeSdkGroupClient,
        "SandboxGroupClient",
        "list_snapshots",
        "async_iterable",
        item_type="Snapshot",
    ),
    _OperationShape(
        FakeSdkGroupClient,
        "SandboxGroupClient",
        "begin_delete_snapshot",
        "poller",
        poller_result_type="None",
    ),
    _OperationShape(FakeSdkGroupClient, "SandboxGroupClient", "close", "value"),
)


def _sdk_modules() -> tuple[ModuleType, ModuleType]:
    try:
        installed_version = version("azure-containerapps-sandbox")
        sdk = import_module(_SDK_MODULE)
        async_sdk = import_module(f"{_SDK_MODULE}.aio")
    except (ImportError, PackageNotFoundError):
        pytest.skip("The optional ACA Sandbox SDK is not installed.")
    assert installed_version == _PINNED_VERSION
    return sdk, async_sdk


def _type_key(annotation: object) -> object:
    """Compare resolved annotations without allowing ``Any`` to mask drift."""

    if annotation is None:
        return ("none",)
    if annotation is type(None):
        return ("none-type",)
    origin = get_origin(annotation)
    if origin in (types.UnionType, Union):
        return ("union", tuple(sorted((_type_key(item) for item in get_args(annotation)), key=repr)))
    if origin is Literal:
        return ("literal", get_args(annotation))
    if origin is not None:
        return (
            "generic",
            _type_key(origin),
            tuple(_type_key(item) for item in get_args(annotation)),
        )
    if inspect.isclass(annotation):
        name = _FAKE_TO_REAL.get(annotation.__name__, annotation.__name__)
        return ("class", name)
    return ("other", repr(annotation))


def _parameters(callable_: object) -> tuple[inspect.Parameter, ...]:
    return tuple(
        parameter
        for parameter in inspect.signature(callable_).parameters.values()
        if parameter.name != "self"
    )


def _default_key(default: object) -> tuple[object, ...]:
    if default is inspect.Parameter.empty:
        return ("missing",)
    return ("value", type(default), default)


def _parameter_contract(
    callable_: object,
) -> tuple[tuple[str, inspect._ParameterKind, tuple[object, ...]], ...]:
    return tuple(
        (parameter.name, parameter.kind, _default_key(parameter.default))
        for parameter in _parameters(callable_)
    )


def _field_default_contract(field: Field[object]) -> tuple[object, ...]:
    if field.default is not MISSING:
        return ("value", type(field.default), field.default)
    if field.default_factory is MISSING:
        return ("missing",)
    produced = field.default_factory()
    return ("factory", field.default_factory, type(produced), produced)


def _expected_type_key(name: str) -> object:
    if name == "None":
        return _type_key(type(None))
    return ("class", name)


def _assert_model_shape(shape: _ModelShape, sdk: ModuleType) -> None:
    real = getattr(sdk, shape.real_name)
    fake_fields = tuple(item.name for item in fields(shape.fake))
    assert fake_fields == shape.fields

    fake_parameters = _parameters(shape.fake)
    real_parameters = _parameters(real)
    assert tuple(parameter.name for parameter in fake_parameters) == shape.fields
    assert tuple(
        parameter.name for parameter in real_parameters if parameter.name in shape.fields
    ) == shape.fields

    fake_hints = get_type_hints(shape.fake)
    real_hints = get_type_hints(
        real,
        globalns={**vars(inspect.getmodule(real)), "AsyncLROPoller": AsyncLROPoller},
    )
    real_fields = {item.name: item for item in fields(real)}
    fake_fields_by_name = {item.name: item for item in fields(shape.fake)}
    for fake_parameter in fake_parameters:
        real_parameter = next(
            parameter for parameter in real_parameters if parameter.name == fake_parameter.name
        )
        assert _default_key(fake_parameter.default) == _default_key(real_parameter.default)
        assert _field_default_contract(fake_fields_by_name[fake_parameter.name]) == (
            _field_default_contract(real_fields[fake_parameter.name])
        )
        assert _type_key(fake_hints[fake_parameter.name]) == _type_key(
            real_hints[fake_parameter.name]
        )


def _assert_operation_shape(shape: _OperationShape, async_sdk: ModuleType) -> None:
    fake = getattr(shape.fake_owner, shape.method)
    real = getattr(getattr(async_sdk, shape.real_owner), shape.method)
    assert _parameter_contract(fake) == _parameter_contract(real)
    assert inspect.iscoroutinefunction(fake) == inspect.iscoroutinefunction(real)
    fake_hints = get_type_hints(fake)
    real_hints = get_type_hints(
        real,
        globalns={**vars(inspect.getmodule(real)), "AsyncLROPoller": AsyncLROPoller},
    )
    for parameter in _parameters(fake):
        if parameter.kind is inspect.Parameter.VAR_KEYWORD:
            continue
        assert _type_key(fake_hints[parameter.name]) == _type_key(real_hints[parameter.name])

    if shape.return_shape == "poller":
        assert shape.poller_result_type is not None
        fake_return = fake_hints["return"]
        real_return = real_hints["return"]
        assert get_origin(fake_return) is FakePoller
        assert _type_key(get_args(fake_return)[0]) == _expected_type_key(shape.poller_result_type)
        assert _type_key(real_return) == ("class", "AsyncLROPoller")
        assert get_type_hints(
            FakePoller.result,
            localns={"T": FakePoller.__type_params__[0]},
        )["return"] is FakePoller.__type_params__[0]
    else:
        fake_return = get_type_hints(fake)["return"]
        real_return = real_hints["return"]
    if shape.return_shape == "value":
        assert _type_key(fake_return) == _type_key(real_return)
    elif shape.return_shape == "async_iterable":
        assert shape.item_type is not None
        fake_origin = get_origin(fake_return)
        real_origin = get_origin(real_return)
        assert fake_origin is not None and hasattr(fake_origin, "__aiter__")
        assert real_origin is not None and hasattr(real_origin, "__aiter__")
        assert _type_key(get_args(fake_return)[0]) == _expected_type_key(shape.item_type)
        assert _type_key(get_args(real_return)[0]) == _expected_type_key(shape.item_type)


def test_fake_models_match_pinned_aca_sdk_runtime_shapes() -> None:
    sdk, _ = _sdk_modules()
    for shape in _MODEL_SHAPES:
        _assert_model_shape(shape, sdk)


def test_fake_client_methods_match_pinned_aca_sdk_runtime_shapes() -> None:
    _, async_sdk = _sdk_modules()
    for shape in _OPERATION_SHAPES:
        _assert_operation_shape(shape, async_sdk)


def _operation_shape(owner: type[object], method: str) -> _OperationShape:
    return next(
        shape for shape in _OPERATION_SHAPES if shape.fake_owner is owner and shape.method == method
    )


def _model_shape(fake: type[object]) -> _ModelShape:
    return next(shape for shape in _MODEL_SHAPES if shape.fake is fake)


def test_begin_stop_declares_pinned_polling_timeout_default() -> None:
    _, async_sdk = _sdk_modules()
    fake = inspect.signature(FakeSdkSandboxClient.begin_stop).parameters["polling_timeout"]
    real = inspect.signature(async_sdk.SandboxClient.begin_stop).parameters["polling_timeout"]
    assert fake.default == 180
    assert real.default == 180
    _assert_operation_shape(_operation_shape(FakeSdkSandboxClient, "begin_stop"), async_sdk)


def test_operation_guard_rejects_wrong_scalar_parameter_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, async_sdk = _sdk_modules()

    async def wrong_begin_stop(
        self: FakeSdkSandboxClient,
        *,
        polling_timeout: int = 181,
        polling_interval: int = 3,
        **kwargs: object,
    ) -> FakePoller[None]:
        del self, polling_timeout, polling_interval, kwargs
        return FakePoller(None)

    monkeypatch.setattr(FakeSdkSandboxClient, "begin_stop", wrong_begin_stop)
    with pytest.raises(AssertionError):
        _assert_operation_shape(_operation_shape(FakeSdkSandboxClient, "begin_stop"), async_sdk)


def test_model_guard_rejects_different_default_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    sdk, _ = _sdk_modules()
    entries = FakeSdkDirListing.__dataclass_fields__["entries"]
    monkeypatch.setattr(entries, "default_factory", tuple)

    with pytest.raises(AssertionError):
        _assert_model_shape(_model_shape(FakeSdkDirListing), sdk)


def test_operation_guard_rejects_wrong_poller_result_type(monkeypatch: pytest.MonkeyPatch) -> None:
    _, async_sdk = _sdk_modules()

    async def wrong_begin_stop(
        self: FakeSdkSandboxClient,
        *,
        polling_timeout: int = 180,
        polling_interval: int = 3,
        **kwargs: object,
    ) -> FakePoller[str]:
        del self, polling_timeout, polling_interval, kwargs
        return FakePoller("not-none")

    monkeypatch.setattr(FakeSdkSandboxClient, "begin_stop", wrong_begin_stop)
    with pytest.raises(AssertionError):
        _assert_operation_shape(_operation_shape(FakeSdkSandboxClient, "begin_stop"), async_sdk)


def test_operation_guard_rejects_wrong_async_iterator_item_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, async_sdk = _sdk_modules()

    def wrong_list_sandboxes(
        self: FakeSdkGroupClient,
        *,
        labels: dict[str, str] | None = None,
        **kwargs: object,
    ) -> AsyncIterator[str]:
        del self, labels, kwargs

        async def iterate() -> AsyncIterator[str]:
            yield "not-a-sandbox"

        return iterate()

    monkeypatch.setattr(FakeSdkGroupClient, "list_sandboxes", wrong_list_sandboxes)
    with pytest.raises(AssertionError):
        _assert_operation_shape(_operation_shape(FakeSdkGroupClient, "list_sandboxes"), async_sdk)


def test_fake_factory_methods_accept_the_required_sdk_constructor_contract() -> None:
    _, async_sdk = _sdk_modules()
    for factory, client_name, required_names in (
        (
            FakeSdkEnvironment.make_group_client,
            "SandboxGroupClient",
            ("endpoint", "credential", "subscription_id", "resource_group", "sandbox_group"),
        ),
        (
            FakeSdkEnvironment.make_sandbox_client,
            "SandboxClient",
            (
                "endpoint",
                "credential",
                "subscription_id",
                "resource_group",
                "sandbox_group",
                "sandbox_id",
            ),
        ),
    ):
        fake_parameters = {parameter.name: parameter for parameter in _parameters(factory)}
        real_parameters = {
            parameter.name: parameter
            for parameter in _parameters(getattr(async_sdk, client_name))
        }
        for name in required_names:
            assert name in fake_parameters
            assert name in real_parameters
            assert fake_parameters[name].kind == real_parameters[name].kind
            assert (fake_parameters[name].default is inspect.Parameter.empty) == (
                real_parameters[name].default is inspect.Parameter.empty
            )


@pytest.mark.parametrize(
    ("fake", "real_owner", "member"),
    (
        (FakeSdkSandboxSummary, "Sandbox", "modified_at"),
        (FakeSdkSnapshot, "Snapshot", "created_at"),
        (FakeSdkAutoSuspendPolicy, "AutoSuspendPolicy", "auto_suspend_seconds"),
        (FakeSdkSandboxClient, "SandboxClient", "get_lifecycle_policy"),
    ),
)
def test_fake_does_not_retain_known_non_sdk_members(
    fake: type[object], real_owner: str, member: str
) -> None:
    sdk, async_sdk = _sdk_modules()
    real = getattr(async_sdk if real_owner.endswith("Client") else sdk, real_owner)
    assert not hasattr(real, member)
    assert not hasattr(fake, member)
