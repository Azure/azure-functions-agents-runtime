from __future__ import annotations

from types import SimpleNamespace

import pytest
from durabletask.task import CompletableTask, TaskFailedError

from azure_functions_agents.workflows._wave_execution import (
    ContinuationWaveStrategy,
    PerTaskWaveStrategy,
    WaveTaskSpec,
    select_wave_strategy,
)

STRATEGIES = [PerTaskWaveStrategy, ContinuationWaveStrategy]


@pytest.mark.parametrize("continuation", [False, True])
def test_strategy_selection_uses_stored_policy(continuation):
    specs = [WaveTaskSpec("first"), WaveTaskSpec("second", continue_on_error=continuation)]
    strategy = select_wave_strategy(specs)
    expected = ContinuationWaveStrategy if continuation else PerTaskWaveStrategy
    assert type(strategy) is expected


@pytest.mark.parametrize("strategy_type", STRATEGIES)
def test_empty_wave_finishes_without_a_wait(strategy_type):
    generator = strategy_type().wait(None, CompletableTask(), [], [])
    with pytest.raises(StopIteration) as stopped:
        next(generator)
    assert stopped.value.value == []


@pytest.mark.parametrize("strategy_type", STRATEGIES)
def test_each_strategy_preserves_result_order(strategy_type):
    strategy = strategy_type()
    cancel = CompletableTask()
    first, second = CompletableTask(), CompletableTask()
    specs = [WaveTaskSpec("first"), WaveTaskSpec("second")]
    generator = strategy.wait(None, cancel, [first, second], specs)
    race = next(generator)
    second.complete({"result": "second"})
    race = generator.send(race.result)
    assert not race.is_complete
    first.complete({"result": "first"})
    with pytest.raises(StopIteration) as stopped:
        generator.send(race.result)
    assert [
        strategy.resolve_result(spec, raw)
        for spec, raw in zip(specs, stopped.value.value, strict=True)
    ] == ["first", "second"]


@pytest.mark.parametrize("strategy_type", STRATEGIES)
@pytest.mark.parametrize("completed", ["cancel", "child", "both"])
def test_each_strategy_handles_precompleted_candidates(strategy_type, completed):
    cancel = CompletableTask()
    child = CompletableTask()
    if completed in {"cancel", "both"}:
        cancel.complete("stop")
    if completed in {"child", "both"}:
        child.complete({"result": "done"})
    strategy = strategy_type()
    spec = WaveTaskSpec("child")
    generator = strategy.wait(None, cancel, [child], [spec])
    race = next(generator)
    assert race.is_complete
    with pytest.raises(StopIteration) as stopped:
        generator.send(race.result)
    if completed == "child":
        assert strategy.resolve_result(spec, stopped.value.value[0]) == "done"
    else:
        assert stopped.value.value is None


@pytest.mark.parametrize("strategy_type", STRATEGIES)
def test_each_strategy_rejects_an_unknown_winner(strategy_type):
    generator = strategy_type().wait(
        None, CompletableTask(), [CompletableTask()], [WaveTaskSpec("child")],
    )
    next(generator)
    with pytest.raises(RuntimeError, match="unknown task"):
        generator.send(CompletableTask())


def test_ordinary_strategy_defers_terminal_result_failure():
    strategy = PerTaskWaveStrategy()
    first, second = CompletableTask(), CompletableTask()
    specs = [WaveTaskSpec("first", policy_aware=True), WaveTaskSpec("second")]
    generator = strategy.wait(None, CompletableTask(), [first, second], specs)
    race = next(generator)
    first.complete({
        "id": "first",
        "ok": False,
        "failure": {
            "error_code": "dependency_failed",
            "error": "Dependency failed.",
            "kind": "handler_terminal",
            "retryable": False,
        },
    })
    race = generator.send(race.result)
    assert not race.is_complete
    second.complete({"result": "second"})
    with pytest.raises(StopIteration) as stopped:
        generator.send(race.result)
    with pytest.raises(RuntimeError, match="dependency_failed"):
        strategy.resolve_result(specs[0], stopped.value.value[0])


@pytest.mark.parametrize("strategy_type", STRATEGIES)
def test_each_strategy_resolves_timer_results(strategy_type):
    assert strategy_type().resolve_result(
        WaveTaskSpec("pause", deadline="2026-10-30T00:00:00+00:00"), None,
    ) == {"waited_until": "2026-10-30T00:00:00+00:00"}


def test_continuation_keeps_an_already_resolved_business_result():
    result = {"result": "business value", "failed": False}
    strategy = ContinuationWaveStrategy()
    assert strategy.resolve_result(WaveTaskSpec("first"), result) is result
    error = RuntimeError("already decoded")
    assert strategy.resolve_failure(SimpleNamespace(), [], error) is error


def test_unclassified_activity_failure_logs_host_guidance(caplog):
    context = SimpleNamespace(instance_id="workflow-parent", is_replaying=False)
    secret = "raw host failure detail"
    failure = TaskFailedError("opaque durable failure", RuntimeError(secret))
    specs = [WaveTaskSpec("probe", policy_aware=True, timeout_ms=30_000)]

    with caplog.at_level("WARNING"):
        assert PerTaskWaveStrategy().resolve_failure(context, specs, failure) is failure

    assert "unclassified Activity failure" in caplog.text
    assert "host.json" in caplog.text
    assert "AzureFunctionsJobHost__functionTimeout" in caplog.text
    assert "workflow_id=workflow-parent" in caplog.text
    assert "node_id=probe" in caplog.text
    assert "timeout_ms=30000" in caplog.text
    assert secret not in caplog.text


def test_unclassified_activity_failure_warning_is_suppressed_on_replay(
    caplog,
):
    context = SimpleNamespace(instance_id="workflow-parent", is_replaying=True)
    failure = TaskFailedError("opaque durable failure", RuntimeError("detail"))

    with caplog.at_level("WARNING"):
        assert PerTaskWaveStrategy().resolve_failure(
            context, [WaveTaskSpec("probe", policy_aware=True, timeout_ms=30_000)], failure,
        ) is failure

    assert "unclassified Activity failure" not in caplog.text


def test_failure_warning_does_not_count_timers(caplog):
    context = SimpleNamespace(instance_id="workflow-parent", is_replaying=False)
    failure = TaskFailedError("opaque durable failure", RuntimeError("detail"))
    specs = [
        WaveTaskSpec("pause", policy_aware=True, deadline="2026-10-30T00:00:00+00:00"),
        WaveTaskSpec("probe", policy_aware=True, timeout_ms=30_000),
    ]

    with caplog.at_level("WARNING"):
        assert PerTaskWaveStrategy().resolve_failure(context, specs, failure) is failure

    assert "node_id=probe" in caplog.text
    assert "timeout_ms=30000" in caplog.text


@pytest.mark.parametrize("strategy_type", STRATEGIES)
def test_durable_failure_preserves_its_cause_without_waiting_for_siblings(strategy_type):
    first, second = CompletableTask(), CompletableTask()
    generator = strategy_type().wait(
        None, CompletableTask(), [first, second], [WaveTaskSpec("first"), WaveTaskSpec("second")],
    )
    race = next(generator)
    first.fail("probe failed", RuntimeError("probe failed"))

    with pytest.raises(TaskFailedError) as raised:
        generator.send(race.result)

    assert raised.value is first.get_exception()
    assert not second.is_complete
