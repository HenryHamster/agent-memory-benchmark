# Pilot configuration and troubleshooting

## Configuration and limits

`scripts/pilot/defaults.json` specifies **gemini-3.1-flash-lite** for memory
extraction, answering and judging, with minimal thinking. Embeddings are local
`intfloat/multilingual-e5-small` ONNX; reranking uses FlashRank MiniLM-L12.
Observations/consolidation are off. Answer/judge configuration uses `OMB_ANSWER_*`
and `OMB_JUDGE_*`; upstream `--llm` does not control those effective settings.

The loopback gateway is the only process holding the real key. All retries share
`.pilot/accounting/budget.sqlite`: $4.50 maximum, 600 generations, 2M input tokens
and 2M output tokens including thinking. It reserves conservative cost before
sending each request and retains reservations for uncertain failures. Limits are
per checkout, **not an account-wide limit across six machines**. Rerunning does
not reset them. A second complete case is refused if retry headroom does not fit.
Do not delete the ledger to work around that stop. Actual inputs/responses are in
`.pilot/accounting/model-calls/`; keep this directory private and out of Git.

Pricing checked 2026-10-04: $0.25/M input, $1.50/M output including reasoning.
[Vertex pricing](https://cloud.google.com/gemini-enterprise-agent-platform/generative-ai/pricing).
Costs are usage-priced estimates, not billing invoices. Coordinate a team budget
before paid runs; a stored key alone does not grant a new spending allowance.

Measured reference: complete case correct under both judges, 5m38s, $0.38435;
smoke $0.001956. Small variation is expected, and the new wrapper also checks the
model during server startup. One single-session-user case validates the path; it
does **not** reproduce full LongMemEval accuracy. Gemini replaces the official
GPT-4o judge, so even the official-prompt grade is a Gemini-judged pilot result.

## Troubleshooting

- Import/dependency errors: rerun `setup.sh`; use the two environments it creates.
  The fork moves unrelated providers into an optional `all` extra. Do not run
  `uv sync --extra all` for this task. `uv run --no-sync` with the prepared AMB
  environment is the underlying CLI path; the wrapper supplies it.
- API/model errors: check the chosen key type and `model-availability.json` under
  `.pilot/accounting/`. The wrapper does not enable cloud APIs or change key restrictions.
- Startup failure: inspect the run's `hindsight.log`; first model downloads can
  take minutes. A `--check` run disables LLM verification and confirms API/database readiness only.
- Budget stop: inspect the persistent ledger/usage, preserve failed outputs and
  report the stop. Re-running with different names must not bypass aggregate limits.

See [the implementation notes](PILOT_CHANGES.md) for exact pins and changes.
