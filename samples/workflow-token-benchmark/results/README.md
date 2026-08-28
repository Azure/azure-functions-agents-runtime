# Benchmark results

Named subdirectories contain shareable outputs from disclosed benchmark runs:

- `benchmark.json`: paired token, report-latency, and deterministic-quality data;
- `atif/`: per-mode ATIF trajectories used for offline quality grading;
- `vally/`: optional Vally prompt-judge JSONL verdicts.

Host logs, emulator state, credentials, and `local.settings.json` must not be
committed. Record the model, date, command, Vally version, and judge model in
the pull request or an adjacent run note.
