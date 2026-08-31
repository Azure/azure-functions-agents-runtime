"""Submit benchmark queue messages to an already-running local Functions host."""

from __future__ import annotations

import argparse
import contextlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from azure.core.exceptions import ResourceExistsError
from azure.storage.queue import QueueClient, TextBase64EncodePolicy

SAMPLE_ROOT = Path(__file__).resolve().parents[1]
LOCAL_SETTINGS = SAMPLE_ROOT / "src" / "local.settings.json"
QUEUE_NAMES = {
    "baseline": "token-benchmark-baseline",
    "workflow": "token-benchmark-workflow",
}


def _development_storage_connection() -> str:
    account = "devstoreaccount1"
    key = (
        "Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/"
        "K1SZFPTOtr/KBHBeksoGMGw=="
    )
    return (
        "DefaultEndpointsProtocol=http;"
        f"AccountName={account};AccountKey={key};"
        f"BlobEndpoint=http://127.0.0.1:10000/{account};"
        f"QueueEndpoint=http://127.0.0.1:10001/{account};"
        f"TableEndpoint=http://127.0.0.1:10002/{account};"
    )


def _storage_connection() -> str:
    if not LOCAL_SETTINGS.exists():
        raise RuntimeError(
            "src/local.settings.json is required; copy local.settings.template.json first"
        )
    data = json.loads(LOCAL_SETTINGS.read_text(encoding="utf-8"))
    values = data.get("Values")
    if not isinstance(values, dict):
        raise RuntimeError("local.settings.json must contain a Values object")
    connection = values.get("AzureWebJobsStorage")
    if not isinstance(connection, str) or not connection.strip():
        raise RuntimeError("AzureWebJobsStorage is missing from local.settings.json")
    if connection.strip().lower() == "usedevelopmentstorage=true":
        return _development_storage_connection()
    return connection.strip()


def build_requests(
    *,
    trial_id: str,
    service_count: int,
    evidence_lines: int,
    modes: Sequence[str],
) -> list[tuple[str, dict[str, Any]]]:
    services = [f"checkout-api-{index:02d}" for index in range(service_count)]
    return [
        (
            mode,
            {
                "trial_id": trial_id,
                "services": services,
                "evidence_lines": evidence_lines,
                "report_blob": f"runs/{trial_id}/{mode}.json",
            },
        )
        for mode in modes
    ]


def _send(connection: str, mode: str, request: dict[str, Any]) -> None:
    queue = QueueClient.from_connection_string(
        connection,
        QUEUE_NAMES[mode],
        message_encode_policy=TextBase64EncodePolicy(),
    )
    try:
        with contextlib.suppress(ResourceExistsError):
            queue.create_queue()
        queue.send_message(json.dumps(request, separators=(",", ":")))
    finally:
        queue.close()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("baseline", "workflow", "both"),
        default="both",
    )
    parser.add_argument("--service-count", type=int, default=3)
    parser.add_argument("--evidence-lines", type=int, default=40)
    parser.add_argument("--trial-id")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if not 1 <= args.service_count <= 20:
        print("FAIL: --service-count must be between 1 and 20")
        return 1
    if not 1 <= args.evidence_lines <= 200:
        print("FAIL: --evidence-lines must be between 1 and 200")
        return 1

    trial_id = args.trial_id or datetime.now(UTC).strftime("manual-%Y%m%dT%H%M%SZ")
    modes = ("baseline", "workflow") if args.mode == "both" else (args.mode,)
    connection = _storage_connection()
    requests = build_requests(
        trial_id=trial_id,
        service_count=args.service_count,
        evidence_lines=args.evidence_lines,
        modes=modes,
    )
    for mode, request in requests:
        _send(connection, mode, request)
        print(f"Sent {mode} trial to queue {QUEUE_NAMES[mode]!r}")
        print(f"  report Blob: token-benchmark-reports/{request['report_blob']}")

    print()
    print("Watch the `func start` terminal for:")
    print("  Agent token usage: {...}")
    print("  Agent token usage detail: {...}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
