# Pilot changes and evidence

Base AMB: `f618ed7b1f0eb9cad7b42e876f91a42f0eadb150`.
Hindsight: `HenryHamster/hindsight@f7dd3f4fd7420f7beec60c32c965e5e5cf7be066`, editable source.
Official scorer: `xiaowu0162/LongMemEval@9e0b455f4ef0e2ab8f2e582289761153549043fc`.
Dataset: `xiaowu0162/longmemeval-cleaned@98d7416c24c778c2fee6e6f3006e7a073259d48f`,
`longmemeval_s_cleaned.json`, SHA-256
`d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442`.
Package versions are frozen in `scripts/pilot/*-requirements.txt` (Python 3.12/Linux).

Changes to AMB:

1. Defer imports for unused Mem0 and Qdrant/transformer providers, and make the
   HTTP provider import `hindsight-client` rather than `hindsight-all`.
2. Move broad backend dependencies to the optional `all` extra; expose a small
   `hindsight-http` extra. Normal CLI imports work with the minimal environment.
3. Add `AMB_BANK_PREFIX` so unique output names also map to isolated memory banks.
4. Preserve the time of day in LongMemEval timestamps. Preserve duplicate session
   occurrences with distinct IDs, consistently including gold retrieval IDs.
5. Add the local setup/run wrapper, persistent budget gateway, evidence capture
   and regression tests. The wrapper exports RAG `context`, since `raw_response`
   can be null. `.env` overrides are disabled inside guarded child processes.

Hindsight's engine and AMB's stock answer/judge prompts are unchanged. The runner
extracts the unmodified official `get_anscheck_prompt` from the pinned scorer and
uses Gemini to grade the saved prediction separately, including the official
abstention branch when relevant. That model substitution limits comparability.
The real reference case has no abstention or duplicate-session-ID requirement;
those branches have offline regression coverage only.

The original local pilot exercised real API/database readiness, two-document
isolation and all 53 sessions of case e47becba. Both grades were correct. Total
259 generations: 1,032,677 input and 85,422 output/thinking tokens, $0.38630225.
No full benchmark, V2 or Mem2ActBench was run. Original evidence and secrets are
not published in this fork; each developer saves their own local run artifacts.

The packaged setup was then tested in fresh environments on the same WSL machine:
194 Hindsight packages and 61 AMB packages passed dependency checks; 16 regression
tests passed. The new wrapper's key-free cold readiness check took 189 seconds;
its real isolation check passed in 6.19 seconds, with clean automatic shutdown.
That follow-up used three generations (including startup verification), $0.0019285,
against the original shared ledger. Total pilot + packaging validation: $0.38823075.
The new wrapper's complete-case orchestration is covered offline; no second full
paid case or second-machine reproduction was performed during packaging.
