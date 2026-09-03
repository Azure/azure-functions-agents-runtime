import argparse

import pytest
from eng.scripts.hybrid_spike_benchmark import _percentiles, _run


def test_hybrid_benchmark_percentiles_use_nearest_rank() -> None:
    values = _percentiles([0.001, 0.002, 0.003, 0.004])

    assert values.p50 == 2
    assert values.p95 == 4
    assert values.p99 == 4


@pytest.mark.asyncio
async def test_hybrid_benchmark_requires_explicit_25_approval() -> None:
    args = argparse.Namespace(
        concurrency=25,
        requests=1,
        allow_25=False,
    )

    with pytest.raises(ValueError, match="stable evidence"):
        await _run(args)
