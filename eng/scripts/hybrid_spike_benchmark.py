"""Run bounded hybrid-spike HTTP load and emit aggregate JSON evidence."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import aiohttp

_MAX_CONCURRENCY = 25
_STABLE_DEFAULT_CAP = 10
_DEFAULT_TIMEOUT_SECONDS = 1200


@dataclass(frozen=True, slots=True)
class RequestMeasurement:
    status: int
    first_event_seconds: float
    total_seconds: float
    succeeded: bool


@dataclass(frozen=True, slots=True)
class Quantiles:
    p50: float
    p95: float
    p99: float


def _percentiles(values: list[float]) -> Quantiles:
    if not values:
        raise ValueError("At least one latency value is required.")
    ordered = sorted(value * 1000 for value in values)
    result = [
        ordered[math.ceil(percentile * len(ordered) / 100) - 1]
        for percentile in (50, 95, 99)
    ]
    return Quantiles(*result)


async def _request(
    session: aiohttp.ClientSession,
    *,
    url: str,
    prompt: str,
    function_key: str | None,
    stream: bool,
) -> RequestMeasurement:
    headers = {"content-type": "application/json"}
    if function_key:
        headers["x-functions-key"] = function_key
    started = time.perf_counter()
    first_event: float | None = None
    succeeded = False
    async with session.post(url, headers=headers, json={"prompt": prompt}) as response:
        async for chunk in response.content.iter_any():
            if chunk and first_event is None:
                first_event = time.perf_counter() - started
            if not stream:
                continue
        succeeded = response.status < 400
        total = time.perf_counter() - started
        return RequestMeasurement(
            status=response.status,
            first_event_seconds=first_event if first_event is not None else total,
            total_seconds=total,
            succeeded=succeeded,
        )


async def _run(args: argparse.Namespace) -> dict[str, object]:
    concurrency = int(args.concurrency)
    requests = int(args.requests)
    if not 1 <= concurrency <= _MAX_CONCURRENCY:
        raise ValueError(f"concurrency must be between 1 and {_MAX_CONCURRENCY}")
    if concurrency > _STABLE_DEFAULT_CAP and not args.allow_25:
        raise ValueError("concurrency above 10 requires --allow-25 after stable evidence")
    if not 1 <= requests <= _MAX_CONCURRENCY:
        raise ValueError(f"requests must be between 1 and {_MAX_CONCURRENCY}")
    function_key = os.environ.get(args.function_key_env) if args.function_key_env else None
    timeout = aiohttp.ClientTimeout(total=args.timeout)
    connector = aiohttp.TCPConnector(limit=concurrency)
    workload_started = time.perf_counter()
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        semaphore = asyncio.Semaphore(concurrency)

        async def invoke() -> RequestMeasurement:
            async with semaphore:
                return await _request(
                    session,
                    url=args.url,
                    prompt=args.prompt,
                    function_key=function_key,
                    stream=args.stream,
                )

        measurements = await asyncio.gather(*(invoke() for _ in range(requests)))
    workload_seconds = time.perf_counter() - workload_started
    successful = [measurement for measurement in measurements if measurement.succeeded]
    first_values = [measurement.first_event_seconds for measurement in measurements]
    total_values = [measurement.total_seconds for measurement in measurements]
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "mode": "stream" if args.stream else "nonstream",
        "concurrency": concurrency,
        "request_count": requests,
        "success_count": len(successful),
        "error_count": requests - len(successful),
        "status_counts": {
            str(status): sum(1 for item in measurements if item.status == status)
            for status in sorted({item.status for item in measurements})
        },
        "first_event_ms": asdict(_percentiles(first_values)),
        "total_ms": asdict(_percentiles(total_values)),
        "workload_seconds": workload_seconds,
        "throughput_requests_per_second": requests / workload_seconds,
        "cleanup": "verify-with-sandbox-inventory",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--concurrency", type=int, choices=(1, 10, 25), required=True)
    parser.add_argument("--requests", type=int, default=10)
    parser.add_argument("--stream", action="store_true")
    parser.add_argument("--allow-25", action="store_true")
    parser.add_argument("--function-key-env", default="HYBRID_SPIKE_FUNCTION_KEY")
    parser.add_argument("--timeout", type=float, default=_DEFAULT_TIMEOUT_SECONDS)
    return parser


def main() -> int:
    args = _parser().parse_args()
    report = asyncio.run(_run(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
