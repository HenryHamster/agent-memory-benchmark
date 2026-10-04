# Hindsight + LongMemEval: two-hour developer task

Get Hindsight running locally, run one **complete original LongMemEval-S case**,
and explain how its retrieved evidence supports the answer. Plan for 20–40 minutes
of setup, 5–10 minutes for the case, then inspection and a short report. These are
estimates from one machine, not a guarantee across different machines.

Hindsight extracts, stores and retrieves memories. AMB feeds it conversation
histories, asks a question, generates an answer from retrieved context and grades it.

## 1. Set up

Use Linux or Ubuntu WSL2 with Git, Python 3 and pip, internet access, and about
5 GB free disk. Windows users: run inside Ubuntu, preferably under `~/src`.
Tested: Ubuntu 24.04 / Python 3.12 in WSL2. No Docker or GPU needed.
On fresh Ubuntu, prerequisites are `sudo apt install git python3-pip`.

```bash
git clone --depth 1 --branch local-pilot-v1 https://github.com/HenryHamster/agent-memory-benchmark.git
cd agent-memory-benchmark
bash scripts/pilot/setup.sh
```

Setup fetches the pinned Hindsight fork, official scorer and cleaned dataset,
installs two separate environments, and runs regression tests. It makes no paid
calls. Runtime data stays in ignored `.pilot/`; database data uses a unique Linux
directory under `/var/tmp/amb-pilot-<uid>/`.

## 2. Run

For Henry's Vertex AI key:

```bash
bash scripts/pilot/run.sh --api vertex-express
```

The command prompts for the key privately without saving it. For an AI Studio
key, use `--api gemini-developer` instead; that alternate route has not had a
real-key test in this pilot. An existing `GEMINI_API_KEY` environment variable also
works. Agree the team spending allowance before running.

It starts Hindsight's **fork source**, checks API/database readiness and real
bank isolation, then runs case `e47becba` through AMB's **hindsight-http** provider:
all **53 sessions / 550 turns**, answer generation and two judges. It saves evidence
and stops its services; Ctrl+C also cleans up. It never runs all 500 cases.

## 3. Inspect and submit

The printed `.pilot/runs/<run-id>/` directory contains:

- `case-01/receipt.json`: completeness, timing and both judgments.
- `case-01/retrieved-evidence.json`: exact context and source chunks.
- `case-01/predictions.jsonl`, `official-grade.json`: prediction and grading.
- `config.json`, `source.json`, `usage.json`: configuration, revisions and cost.

Submit a short report: commit/model, readiness/isolation outcomes, prediction and
judgments, **one supporting source excerpt**, runtime/cost and any problem. Trace
**retain → recall → answer → judge**. If blocked at two hours, report the exact
failing stage/error and what you tried. Each developer can do this same task.

Reference: correct answer “Business Administration”; case 5m38s; smoke + case
about $0.39. One case validates the path, **not published full-benchmark accuracy**.
Both judges use Gemini, including the official scoring prompt.

The gateway shares a persistent $4.50 / 600-request / 2M-input / 2M-output-token
ceiling across runs of this checkout, including retries/thinking. This is not a
team-wide cap. Do not delete its ledger to retry; report a budget stop.

Need help? [Configuration and troubleshooting](PILOT_REFERENCE.md),
[exact pins and changes](PILOT_CHANGES.md). `bash scripts/pilot/run.sh --check`
verifies local startup without a key or paid calls; `--smoke-only` runs just the
real isolation test. Standard runs use Gemini 3.1 Flash-Lite for all LLM roles.
