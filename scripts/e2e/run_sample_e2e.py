"""CLI entrypoint for running one sample through the Python E2E harness."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import yaml  # type: ignore[import-untyped]

try:
    from scripts.e2e import expectations, harness, redaction, settings
    from scripts.e2e.expectations import Invocation, SampleExpectations

    from azure_functions_agents.registration._naming import _function_name_from_source
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts.e2e import expectations, harness, redaction, settings
    from scripts.e2e.expectations import Invocation, SampleExpectations

    from azure_functions_agents.registration._naming import _function_name_from_source

LOGGER = logging.getLogger(__name__)
PLACEHOLDER_PREFIX = "${"
PLACEHOLDER_SUFFIX = "}"
ADO_ERROR = "error"
ADO_WARNING = "warning"
REDACTION_SWEEP_GLOBS: tuple[str, ...] = ("*.log", "*.json", "*.txt")
UNREDACTED_QUARANTINE_SUFFIX = ".UNREDACTED-DO-NOT-PUBLISH"
REDACTION_FAILURE_PLACEHOLDER = "[REDACTED — sweep failed: see harness stderr]\n"

type JsonPrimitive = str | int | float | bool | None
type JsonValue = JsonPrimitive | dict[str, "JsonValue"] | list["JsonValue"]


@dataclass(frozen=True, slots=True)
class RunContext:
    sample: SampleExpectations
    sample_path: Path
    sample_artifacts_dir: Path
    transcripts_dir: Path
    func_log_path: Path
    func_json_path: Path
    admin_functions_path: Path
    summary_path: Path
    harness_log_path: Path
    azurite_workdir: Path
    azurite_log_path: Path


def build_parser() -> argparse.ArgumentParser:
    """Build the run-sample CLI parser."""

    parser = argparse.ArgumentParser(
        description="Run one Azure Functions sample end-to-end via the Python harness."
    )
    parser.add_argument("--sample-name", required=True)
    parser.add_argument("--sample-path", required=True)
    parser.add_argument("--artifacts-dir", required=True)
    parser.add_argument("--func-port", type=int, default=7071)
    parser.add_argument("--ready-timeout", type=int, default=180)
    parser.add_argument("--invocation-timeout", type=int, default=600)
    parser.add_argument("--admin-completion-timeout", type=int, default=300)
    parser.add_argument("--no-azurite", action="store_true")
    parser.add_argument("--ado-summary", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the requested sample and return a process exit code."""

    args = build_parser().parse_args(argv)

    try:
        sample = expectations.for_sample(args.sample_name)
    except KeyError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    sample_path = Path(args.sample_path)
    if not sample_path.is_dir():
        print(f"Sample path does not exist: {sample_path}", file=sys.stderr)
        return 2

    context = _build_context(
        sample=sample,
        sample_path=sample_path,
        artifacts_dir=Path(args.artifacts_dir),
    )
    _configure_logging(context.harness_log_path)

    expected_sample_path = Path(sample.sample_path)
    if expected_sample_path != sample_path:
        warning_message = (
            f"Sample path mismatch for {sample.name!r}: expectations.py says "
            f"{expected_sample_path}, CLI says {sample_path}. Proceeding with CLI path."
        )
        LOGGER.warning(warning_message)
        _emit_ado_issue(ADO_WARNING, warning_message)

    start_time = time.monotonic()
    func_process: harness.FuncProcess | None = None
    azurite_process: Any | None = None
    invocation_results: list[harness.InvocationResult] = []
    error_messages: list[str] = []
    admin_functions_metadata: dict[str, Any] = {}
    expected_function_names = sample.expected_function_names
    actual_function_names: frozenset[str] = frozenset()
    exit_code = 0
    fatal_exception: BaseException | None = None
    redactor: redaction.Redactor | None = None
    had_non_empty_secret_input = False

    try:
        env_check = settings.check_required_env(
            sample_name=sample.name,
            required_env_vars=sample.required_env_vars,
        )
        settings_snapshot_path = settings.write_redacted_settings(
            sample_name="",
            env_var_names=sample.required_env_vars,
            artifacts_dir=context.sample_artifacts_dir,
        )
        LOGGER.info("Wrote redacted settings snapshot to %s", settings_snapshot_path)

        if not env_check.ok:
            missing_message = ", ".join(env_check.missing)
            for missing_var in env_check.missing:
                LOGGER.error("Missing required environment variable: %s", missing_var)
            hard_failure = (
                f"Missing required environment variables for {sample.name}: {missing_message}"
            )
            _record_hard_failure(error_messages, hard_failure)
            return 1

        redactor = redaction.build_redactor(
            [os.environ.get(name, "") for name in settings.DEFAULT_SECRET_NAMES]
        )
        had_non_empty_secret_input = any(
            os.environ.get(name, "").strip() for name in settings.DEFAULT_SECRET_NAMES
        )

        runtime_function_names = harness.introspect_expected_functions(sample_path)
        actual_function_names = runtime_function_names
        if runtime_function_names != expected_function_names:
            drift_message = _format_function_drift(
                expected=expected_function_names,
                actual=runtime_function_names,
            )
            LOGGER.error("Runtime function-name drift detected: %s", drift_message)
            _record_hard_failure(
                error_messages,
                f"Function-name drift for {sample.name}: {drift_message}",
            )
            return 1

        if args.no_azurite:
            warning_message = "Skipping Azurite startup because --no-azurite was supplied."
            LOGGER.warning(warning_message)
            _emit_ado_issue(ADO_WARNING, warning_message)
        else:
            azurite_process = harness.start_azurite(
                workdir=context.azurite_workdir,
                log_path=context.azurite_log_path,
            )
            harness.wait_for_azurite(timeout_seconds=60)

        func_process = harness.start_func(
            sample_path=sample_path,
            port=args.func_port,
            log_path=context.func_log_path,
            json_log_path=context.func_json_path,
        )

        admin_functions_metadata = harness.wait_for_host_ready(
            port=args.func_port,
            expected_function_names=expected_function_names,
            timeout_seconds=args.ready_timeout,
        )
        actual_function_names = _extract_function_names(admin_functions_metadata)
        _write_json(
            context.admin_functions_path,
            admin_functions_metadata,
        )
        LOGGER.info("Wrote admin functions metadata to %s", context.admin_functions_path)

        if actual_function_names != expected_function_names:
            drift_message = _format_function_drift(
                expected=expected_function_names,
                actual=actual_function_names,
            )
            _record_hard_failure(
                error_messages,
                f"Host-reported function-name drift for {sample.name}: {drift_message}",
            )
            exit_code = 1

        for skipped_function_name in sorted(sample.skip_invocation_function_names):
            function_entry = _find_function_entry(admin_functions_metadata, skipped_function_name)
            if function_entry is None:
                hard_failure = (
                    f"Expected skip-only function {skipped_function_name!r} was not present in "
                    "/admin/functions metadata."
                )
                _record_hard_failure(error_messages, hard_failure)
                exit_code = 1
                continue

            binding_type = _extract_binding_type(function_entry) or "unknown"
            LOGGER.info(
                "Validated skip-only function %s with binding type %s",
                skipped_function_name,
                binding_type,
            )

        for index, invocation in enumerate(sample.invocations, start=1):
            result = _run_invocation(
                invocation=invocation,
                sample_path=sample_path,
                func_log_path=context.func_log_path,
                func_port=args.func_port,
                invocation_timeout=args.invocation_timeout,
                admin_completion_timeout=args.admin_completion_timeout,
            )
            invocation_results.append(result)

            _write_transcript(
                transcript_path=context.transcripts_dir
                / f"{index:02d}-{result.function_name}.json",
                index=index,
                result=result,
                redactor=redactor,
            )

            if not result.success:
                exit_code = 1
                error_text = result.error or "Invocation failed without an error message."
                _record_hard_failure(
                    error_messages,
                    f"{result.function_name}: {error_text}",
                )

    except KeyboardInterrupt as exc:
        exit_code = 1
        fatal_exception = exc
        message = "Interrupted by operator."
        LOGGER.error(message)
        _record_hard_failure(error_messages, message)
    except Exception as exc:
        exit_code = 1
        fatal_exception = exc
        LOGGER.exception("E2E harness crashed.")
        _record_hard_failure(error_messages, f"{type(exc).__name__}: {exc}")
    finally:
        try:
            if func_process is not None:
                harness.stop_process(func_process.process, name="Functions host")
        except Exception as exc:
            exit_code = 1
            teardown_message = f"Failed to stop Functions host: {exc}"
            LOGGER.exception(teardown_message)
            error_messages.append(teardown_message)

        try:
            if azurite_process is not None:
                harness.stop_process(azurite_process, name="Azurite")
        except Exception as exc:
            exit_code = 1
            teardown_message = f"Failed to stop Azurite: {exc}"
            LOGGER.exception(teardown_message)
            error_messages.append(teardown_message)

        total_duration = time.monotonic() - start_time

        _write_summary(
            summary_path=context.summary_path,
            sample_name=sample.name,
            expected_function_count=len(expected_function_names),
            actual_function_count=len(actual_function_names),
            invocation_results=invocation_results,
            error_messages=error_messages,
            total_duration=total_duration,
            redactor=redactor,
        )
        LOGGER.info("Wrote summary to %s", context.summary_path)
        if args.ado_summary:
            print(f"##vso[task.uploadsummary]{context.summary_path.resolve()}")

        logging.shutdown()
        try:
            if redactor is not None:
                had_matching_artifact_content = _artifacts_have_content(
                    context.sample_artifacts_dir,
                    include_globs=REDACTION_SWEEP_GLOBS,
                )
                replacement_counts = redactor.redact_directory(
                    context.sample_artifacts_dir,
                    include_globs=REDACTION_SWEEP_GLOBS,
                )
                for file_path, replacement_count in sorted(replacement_counts.items()):
                    sys.stderr.write(
                        f"Redaction sweep: {replacement_count} replacement(s) in {file_path}\n"
                    )
                if (
                    had_non_empty_secret_input
                    and had_matching_artifact_content
                    and sum(replacement_counts.values()) == 0
                ):
                    _emit_ado_issue(
                        ADO_WARNING,
                        "Redaction sweep completed with 0 replacements despite non-empty "
                        "secret inputs and harness artifacts with content; this can be "
                        "normal when a short-lived run exits before any secret is logged.",
                    )
        except Exception as exc:
            exit_code = 1
            exc_details = "".join(traceback.format_exception(exc))
            sys.stderr.write(exc_details)
            _emit_ado_issue(
                ADO_ERROR,
                f"message=redaction sweep failed: {exc}; refusing to publish artifact "
                "to avoid leaking secrets",
            )
            _quarantine_unredacted_artifacts(context.sample_artifacts_dir)

    if fatal_exception is not None and not isinstance(fatal_exception, KeyboardInterrupt):
        raise fatal_exception
    return 1 if exit_code else 0


def _build_context(*, sample: SampleExpectations, sample_path: Path, artifacts_dir: Path) -> RunContext:
    sample_artifacts_dir = artifacts_dir
    return RunContext(
        sample=sample,
        sample_path=sample_path,
        sample_artifacts_dir=sample_artifacts_dir,
        transcripts_dir=sample_artifacts_dir / "transcripts",
        func_log_path=sample_artifacts_dir / "func.log",
        func_json_path=sample_artifacts_dir / "func.json",
        admin_functions_path=sample_artifacts_dir / "admin-functions.json",
        summary_path=sample_artifacts_dir / "summary.md",
        harness_log_path=sample_artifacts_dir / "harness.log",
        azurite_workdir=sample_artifacts_dir / "azurite-data",
        azurite_log_path=sample_artifacts_dir / "azurite.log",
    )


def _configure_logging(log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)
    logger.addHandler(stdout_handler)

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)


def _run_invocation(
    *,
    invocation: Invocation,
    sample_path: Path,
    func_log_path: Path,
    func_port: int,
    invocation_timeout: int,
    admin_completion_timeout: int,
) -> harness.InvocationResult:
    request_body = _expand_placeholders(invocation.body)
    LOGGER.info(
        "Invoking #%s (%s): %s %s",
        invocation.function_name,
        invocation.kind,
        invocation.method,
        invocation.path,
    )

    if invocation.kind == "http" and invocation.is_sse:
        return harness.invoke_http_sse(
            port=func_port,
            path=invocation.path,
            body=request_body,
            headers=invocation.headers,
            expected_status=invocation.expected_status,
            first_event_timeout=float(invocation_timeout),
            function_name=invocation.function_name,
        )

    if invocation.kind == "http":
        return harness.invoke_http(
            method=invocation.method,
            path=invocation.path,
            body=request_body,
            headers=invocation.headers,
            expected_status=invocation.expected_status,
            port=func_port,
            timeout_seconds=float(invocation_timeout),
            function_name=invocation.function_name,
        )

    if invocation.kind == "admin_function":
        input_value = ""
        if isinstance(request_body, dict):
            input_candidate = request_body.get("input", "")
            input_value = "" if input_candidate is None else str(input_candidate)

        result = harness.invoke_admin_function(
            port=func_port,
            function_name=invocation.function_name,
            input_value=input_value,
            expected_status=invocation.expected_status,
            timeout_seconds=float(invocation_timeout),
        )
        if result.success and invocation.requires_log_completion:
            display_name = _lookup_agent_display_name(sample_path, invocation.function_name)
            try:
                completion = harness.wait_for_log_completion(
                    log_path=func_log_path,
                    function_display_name=display_name,
                    timeout_seconds=float(admin_completion_timeout),
                )
                result.log_completion_lines.extend(completion.matched_lines)
                if completion.status == "failure":
                    result.success = False
                    result.error = "Runtime reported agent failure:\n" + "\n".join(
                        completion.matched_lines
                    )
            except TimeoutError:
                result.success = False
                tail_lines = _tail_lines(func_log_path, limit=50)
                result.error = (
                    f"completion log line not seen within {admin_completion_timeout}s\n"
                    + "\n".join(tail_lines)
                )
        return result

    if invocation.kind == "mcp_webhook":
        method = "tools/list"
        request_id = "1"
        params: dict[str, Any] | None = None
        if isinstance(request_body, dict) and "method" in request_body:
            request_id = str(request_body.get("id", "1"))
            method_value = request_body.get("method", method)
            method = str(method_value)
            params_value = request_body.get("params")
            params = cast(dict[str, Any] | None, params_value) if isinstance(params_value, dict) else {}

        return harness.invoke_mcp_webhook(
            port=func_port,
            method=method,
            request_id=request_id,
            params=params,
            expected_status=invocation.expected_status,
            timeout_seconds=float(invocation_timeout),
            function_name=invocation.function_name,
        )

    raise ValueError(f"Unsupported invocation kind: {invocation.kind}")


def _expand_placeholders(value: JsonValue | None) -> JsonValue | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return {key: _expand_placeholders(nested) for key, nested in value.items()}
    if isinstance(value, list):
        return [_expand_placeholders(item) for item in value]
    if isinstance(value, str):
        if value.startswith(PLACEHOLDER_PREFIX) and value.endswith(PLACEHOLDER_SUFFIX):
            env_name = value[len(PLACEHOLDER_PREFIX) : -len(PLACEHOLDER_SUFFIX)]
            env_value = os.environ.get(env_name)
            if env_value is None or not env_value.strip():
                raise ValueError(
                    f"Environment variable {env_name!r} is required to expand placeholder {value!r}."
                )
            return env_value
        return value
    return value


def _lookup_agent_display_name(sample_path: Path, function_name: str) -> str:
    for agent_file in sorted(sample_path.glob("*.agent.md")):
        resolved_name = _function_name_from_source(
            agent_file.name,
            agent_file.name.removesuffix(".agent.md"),
        )
        if resolved_name != function_name:
            continue

        frontmatter_name = _read_frontmatter_name(agent_file)
        if frontmatter_name:
            return frontmatter_name
        return function_name

    return function_name


def _read_frontmatter_name(agent_file: Path) -> str | None:
    try:
        text = agent_file.read_text(encoding="utf-8")
    except OSError as exc:
        LOGGER.warning("Could not read agent file %s: %s", agent_file, exc)
        return None

    if not text.startswith("---"):
        return None

    try:
        _, frontmatter, _ = text.split("---", 2)
    except ValueError:
        return None

    try:
        payload = yaml.safe_load(frontmatter)
    except yaml.YAMLError as exc:
        LOGGER.warning("Could not parse YAML frontmatter in %s: %s", agent_file, exc)
        return None

    if isinstance(payload, dict):
        name_value = payload.get("name")
        if isinstance(name_value, str) and name_value.strip():
            # Source of truth: registration/_handlers.py logs resolved.name in
            # "Agent '%s' response:" when async execution completes.
            return name_value.strip()
    return None


def _extract_function_names(admin_functions_metadata: dict[str, Any]) -> frozenset[str]:
    names: set[str] = set()
    for entry in _iter_function_entries(admin_functions_metadata):
        if isinstance(entry, dict):
            name_value = entry.get("name")
            if isinstance(name_value, str):
                names.add(name_value)
    return frozenset(names)


def _iter_function_entries(admin_functions_metadata: dict[str, Any]) -> list[object]:
    for key in ("functions", "value", "items"):
        entries = admin_functions_metadata.get(key)
        if isinstance(entries, list):
            return entries
    return []


def _find_function_entry(
    admin_functions_metadata: dict[str, Any],
    function_name: str,
) -> dict[str, Any] | None:
    for entry in _iter_function_entries(admin_functions_metadata):
        if isinstance(entry, dict) and entry.get("name") == function_name:
            return cast(dict[str, Any], entry)
    return None


def _extract_binding_type(function_entry: dict[str, Any]) -> str | None:
    for container_key in ("bindings",):
        bindings = function_entry.get(container_key)
        binding_type = _binding_type_from_bindings(bindings)
        if binding_type is not None:
            return binding_type

    config = function_entry.get("config")
    if isinstance(config, dict):
        return _binding_type_from_bindings(config.get("bindings"))
    return None


def _binding_type_from_bindings(bindings: object) -> str | None:
    if not isinstance(bindings, list):
        return None
    for binding in bindings:
        if isinstance(binding, dict):
            binding_type = binding.get("type")
            if isinstance(binding_type, str):
                return binding_type
    return None


def _write_transcript(
    *,
    transcript_path: Path,
    index: int,
    result: harness.InvocationResult,
    redactor: redaction.Redactor,
) -> None:
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "index": index,
        "function_name": result.function_name,
        "kind": result.kind,
        "method": result.method,
        "path": result.path,
        "request_body": result.request_body,
        "status_code": result.status_code,
        "duration_seconds": result.duration_seconds,
        "success": result.success,
        "response_headers": result.response_headers,
        "response_excerpt": result.response_excerpt,
        "log_completion_lines": result.log_completion_lines,
        "error": result.error,
    }
    transcript_json = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    transcript_path.write_text(redactor.redact_text(transcript_json), encoding="utf-8")


def _write_summary(
    *,
    summary_path: Path,
    sample_name: str,
    expected_function_count: int,
    actual_function_count: int,
    invocation_results: list[harness.InvocationResult],
    error_messages: list[str],
    total_duration: float,
    redactor: redaction.Redactor | None,
) -> None:
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    passed_invocations = sum(1 for result in invocation_results if result.success)

    lines = [
        f"# E2E: {sample_name}",
        "",
        "| metric | value |",
        "|---|---|",
        f"| expected functions | {expected_function_count} |",
        f"| actual functions   | {actual_function_count} |",
        f"| invocations run    | {len(invocation_results)} |",
        f"| invocations passed | {passed_invocations} |",
        f"| total duration     | {_format_duration(total_duration)} |",
        "",
        "## Invocations",
        "",
        "| # | function | kind | status | duration | result |",
        "|---|---|---|---|---|---|",
    ]

    for index, result in enumerate(invocation_results, start=1):
        lines.append(
            "| {index} | {function_name} | {kind} | {status} | {duration} | {outcome} |".format(
                index=index,
                function_name=result.function_name,
                kind=result.kind,
                status=result.status_code if result.status_code is not None else "n/a",
                duration=_format_duration(result.duration_seconds),
                outcome="✅" if result.success else "❌",
            )
        )

    if error_messages:
        lines.extend(["", "## Errors", ""])
        for error_message in error_messages:
            lines.append(f"- {error_message}")

    summary_text = "\n".join(lines) + "\n"
    if redactor is not None:
        summary_text = redactor.redact_text(summary_text)
    summary_path.write_text(summary_text, encoding="utf-8")


def _format_duration(duration_seconds: float) -> str:
    return f"{duration_seconds:.1f}s"


def _format_function_drift(*, expected: frozenset[str], actual: frozenset[str]) -> str:
    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    parts: list[str] = []
    if missing:
        parts.append(f"missing_from_runtime={missing}")
    if unexpected:
        parts.append(f"extra_in_runtime={unexpected}")
    return "; ".join(parts) if parts else "no drift"


def _tail_lines(path: Path, *, limit: int) -> list[str]:
    if not path.exists():
        return [f"<log file missing: {path}>"]
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return lines[-limit:] or ["<log file empty>"]


def _record_hard_failure(error_messages: list[str], message: str) -> None:
    LOGGER.error(message)
    _emit_ado_issue(ADO_ERROR, message)
    error_messages.append(message)


def _artifacts_have_content(root: Path, *, include_globs: tuple[str, ...]) -> bool:
    if not root.is_dir():
        return False

    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if not _matches_any_glob(path.relative_to(root), include_globs):
            continue
        if path.stat().st_size > 0:
            return True
    return False


def _quarantine_unredacted_artifacts(sample_artifacts_dir: Path) -> None:
    quarantined_dir = sample_artifacts_dir.with_name(
        f"{sample_artifacts_dir.name}{UNREDACTED_QUARANTINE_SUFFIX}"
    )
    truncate_root = sample_artifacts_dir

    try:
        if sample_artifacts_dir.exists():
            sample_artifacts_dir.rename(quarantined_dir)
            truncate_root = quarantined_dir
    except Exception as exc:
        sys.stderr.write(
            "Failed to quarantine unredacted artifact directory "
            f"{sample_artifacts_dir}: {exc}\n"
        )
        _emit_ado_issue(
            ADO_ERROR,
            f"message=failed to quarantine unredacted artifact directory "
            f"{sample_artifacts_dir}: {exc}",
        )

    try:
        _truncate_matching_files(truncate_root, include_globs=REDACTION_SWEEP_GLOBS)
    except Exception as exc:
        sys.stderr.write(f"Failed to truncate quarantined artifacts in {truncate_root}: {exc}\n")
        _emit_ado_issue(
            ADO_ERROR,
            f"message=failed to truncate quarantined artifacts in {truncate_root}: {exc}",
        )


def _truncate_matching_files(root: Path, *, include_globs: tuple[str, ...]) -> None:
    if not root.is_dir():
        return

    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if not _matches_any_glob(path.relative_to(root), include_globs):
            continue
        path.write_text(REDACTION_FAILURE_PLACEHOLDER, encoding="utf-8")


def _matches_any_glob(path: Path, globs: tuple[str, ...]) -> bool:
    return any(path.match(glob) for glob in globs)


def _emit_ado_issue(issue_type: str, message: str) -> None:
    sanitized_message = message.replace("\r", " ").replace("\n", " | ")
    print(f"##vso[task.logissue type={issue_type}]{sanitized_message}", flush=True)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    raise SystemExit(main())
