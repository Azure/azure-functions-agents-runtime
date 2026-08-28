# Foundry benchmark: 2026-08-28

This run used the same Microsoft Foundry `gpt-5.4-mini` deployment for both
conditions, 40 evidence lines per service, and three paired repeats at each
workload size. Execution order alternated within the series.

| Services | Valid pairs | Baseline tokens (median) | Workflow tokens (median) | Token reduction (paired median) | Baseline latency ms (median) | Workflow latency ms (median) | Latency reduction (paired median) |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 3/3 | 12,744 | 5,609 | 56.0% | 38,635 | 7,132 | 77.2% |
| 3 | 3/3 | 31,996 | 5,867 | 81.7% | 84,272 | 8,629 | 88.5% |
| 5 | 3/3 | 51,295 | 6,174 | 88.0% | 134,446 | 8,112 | 94.3% |
| 10 | 3/3 | 99,097 | 6,781 | 93.1% | 247,052 | 12,695 | 95.1% |

All 24 mode results scored `1.0` and exact-pass against the deterministic
field-level oracle. Vally 0.14.0 graded repeat 1 for each service count using
`gpt-5.4-mini`: all eight blinded Baseline/Workflow trajectories scored `1.0`
overall and passed every rubric criterion. Three trajectories received 4/5
instead of 5/5 on one presentation-related criterion because the report was raw
JSON; all factual criteria were 5/5. This indicates no observed quality loss in
this structured task; it does not establish quality equivalence for open-ended
tasks.

Commands:

```powershell
python scripts\benchmark.py --service-counts 1 3 5 10 --repeats 3 `
  --evidence-lines 40 --timeout 600 `
  --results-dir results\2026-08-28-foundry

python scripts\grade_quality.py `
  --results-dir results\2026-08-28-foundry --repeat 1 `
  --judge-model gpt-5.4-mini
```

Environment: Azure Functions Core Tools 4.10.0, Python 3.14.0, and Vally
0.14.0. Python 3.14 was selected because the installed Python 3.13.15 worker
exited with Windows access violation `0xC0000005` during function indexing.
The failed startup attempts produced no benchmark observations and are not
included.
