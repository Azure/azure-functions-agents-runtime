"""Wave wait strategies selected from persisted workflow input."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Generator, Sequence
from dataclasses import dataclass
from typing import Any

from durabletask.task import OrchestrationContext, Task, TaskFailedError, when_all, when_any

from azure_functions_agents._logger import logger

from .activity import ActivityFailure, validate_activity_result
from .native_retry import decode_durable_retry_failure
from .schema import WaveWaitMode

type WaveWait = Generator[Task[Any], Task[Any], list[Any] | None]


@dataclass(frozen=True)
class WaveTaskSpec:
    node_id: str
    policy_aware: bool = False
    continue_on_error: bool = False
    timeout_ms: int | None = None
    deadline: str | None = None


def _warn_unclassified_activity_failure(
    context: OrchestrationContext,
    error: BaseException,
    candidates: list[tuple[str, int | None]],
) -> None:
    if (
        not candidates
        or not isinstance(error, TaskFailedError)
        or context.is_replaying
    ):
        return
    node_id: str | None = None
    timeout_ms: int | None = None
    if len(candidates) == 1:
        node_id, timeout_ms = candidates[0]
    logger.warning(
        "The workflow received an unclassified Activity failure. A host timeout "
        "or worker restart may have interrupted the Activity. Check Azure Functions "
        "host logs and Application Insights, if enabled. Compare the effective "
        "functionTimeout setting in host.json or AzureFunctionsJobHost__functionTimeout "
        "with the task timeout. workflow_id=%s node_id=%s timeout_ms=%s",
        context.instance_id,
        node_id or "<unknown>",
        timeout_ms if timeout_ms is not None else "<unknown>",
    )


class WaveExecutionStrategy(ABC):
    """Keep wait timing, failure decoding, and result interpretation together."""

    @abstractmethod
    def wait(
        self,
        context: OrchestrationContext,
        cancel_task: Task[Any],
        wave_tasks: list[Task[Any]],
        specs: Sequence[WaveTaskSpec],
    ) -> WaveWait:
        """Return ordered outcomes, or None when cancellation wins."""
        raise NotImplementedError

    def resolve_result(self, spec: WaveTaskSpec, raw: Any) -> Any:
        if spec.deadline is not None:
            return {"waited_until": spec.deadline}
        if not spec.policy_aware:
            return raw["result"]
        succeeded, outcome = validate_activity_result(spec.node_id, raw)
        if succeeded:
            return outcome
        raise RuntimeError(
            f"task {spec.node_id!r}: {outcome['error']} ({outcome['error_code']})"
        )

    def resolve_failure(
        self,
        context: OrchestrationContext,
        specs: Sequence[WaveTaskSpec],
        error: BaseException,
    ) -> BaseException:
        for spec in specs:
            if not spec.policy_aware:
                continue
            failure = decode_durable_retry_failure(spec.node_id, error)
            if failure is not None:
                return RuntimeError(
                    f"task {spec.node_id!r}: {failure['error']} ({failure['error_code']})"
                )
        _warn_unclassified_activity_failure(
            context,
            error,
            [
                (spec.node_id, spec.timeout_ms)
                for spec in specs
                if spec.policy_aware and spec.deadline is None
            ],
        )
        return error


class GroupedWaveStrategy(WaveExecutionStrategy):
    def wait(
        self,
        context: OrchestrationContext,
        cancel_task: Task[Any],
        wave_tasks: list[Task[Any]],
        specs: Sequence[WaveTaskSpec],
    ) -> WaveWait:
        if not wave_tasks:
            return []
        all_task = when_all(wave_tasks)
        winner = yield when_any([cancel_task, all_task])
        if winner is cancel_task:
            return None
        if winner is not all_task:
            raise RuntimeError("workflow task selection returned an unknown task")
        return all_task.result


class LegacyWaveStrategy(WaveExecutionStrategy):
    def wait(
        self,
        context: OrchestrationContext,
        cancel_task: Task[Any],
        wave_tasks: list[Task[Any]],
        specs: Sequence[WaveTaskSpec],
    ) -> WaveWait:
        outcomes: dict[int, Any] = {}
        pending = list(range(len(wave_tasks)))
        while pending:
            winner = yield when_any(
                [cancel_task, *(wave_tasks[index] for index in pending)]
            )
            if winner is cancel_task:
                return None
            completed = next(
                (index for index in pending if wave_tasks[index] is winner),
                None,
            )
            if completed is None:
                raise RuntimeError("workflow task selection returned an unknown task")
            pending.remove(completed)
            outcomes[completed] = self._read_completed(
                context, specs[completed], wave_tasks[completed],
            )
        return [outcomes[index] for index in range(len(wave_tasks))]

    def _read_completed(
        self,
        context: OrchestrationContext,
        spec: WaveTaskSpec,
        task: Task[Any],
    ) -> Any:
        return task.result


_CONTINUABLE_FAILURE_KINDS = frozenset({
    "handler_transient",
    "handler_terminal",
    "execution_unknown",
})


def _resolve_continuation_failure(
    spec: WaveTaskSpec, failure: ActivityFailure,
) -> dict[str, Any]:
    if spec.continue_on_error and failure["kind"] in _CONTINUABLE_FAILURE_KINDS:
        return {
            "failed": True,
            "error_code": failure["error_code"],
            "error": failure["error"],
            "kind": failure["kind"],
        }
    raise RuntimeError(
        f"task {spec.node_id!r}: {failure['error']} ({failure['error_code']})"
    )


class ContinuationWaveStrategy(LegacyWaveStrategy):
    def _read_completed(
        self,
        context: OrchestrationContext,
        spec: WaveTaskSpec,
        task: Task[Any],
    ) -> Any:
        if spec.deadline is not None:
            return task.result
        try:
            raw = task.result
        except Exception as exc:
            failure = (
                decode_durable_retry_failure(spec.node_id, exc)
                if spec.policy_aware
                else None
            )
            if failure is None:
                if spec.policy_aware:
                    _warn_unclassified_activity_failure(
                        context, exc, [(spec.node_id, spec.timeout_ms)],
                    )
                raise
            return _resolve_continuation_failure(spec, failure)
        if not spec.policy_aware:
            return raw["result"]
        succeeded, outcome = validate_activity_result(spec.node_id, raw)
        if succeeded:
            return outcome
        return _resolve_continuation_failure(spec, outcome)

    def resolve_result(self, spec: WaveTaskSpec, raw: Any) -> Any:
        if spec.deadline is not None:
            return super().resolve_result(spec, raw)
        return raw

    def resolve_failure(
        self,
        context: OrchestrationContext,
        specs: Sequence[WaveTaskSpec],
        error: BaseException,
    ) -> BaseException:
        return error


_ORDINARY_STRATEGIES: dict[WaveWaitMode | None, WaveExecutionStrategy] = {
    None: LegacyWaveStrategy(),
    WaveWaitMode.ALL: GroupedWaveStrategy(),
}
_CONTINUATION_STRATEGY = ContinuationWaveStrategy()


def select_wave_strategy(
    wait_mode: WaveWaitMode | None,
    specs: Sequence[WaveTaskSpec],
) -> WaveExecutionStrategy:
    if any(spec.continue_on_error for spec in specs):
        return _CONTINUATION_STRATEGY
    return _ORDINARY_STRATEGIES[wait_mode]
