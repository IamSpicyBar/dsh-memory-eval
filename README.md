# Memory validation plugin · 0.4.0

This public package contains self-contained L1 evidence/semantic review, optional
L2 content comparison, and L3 validation through the RSI core executor. It does
not clone or import `fuxi-ailabs/memory-eval` or `agent-replay`. No private replay
implementation or generation engine is distributed in this public package.

## Standalone use

Use Python 3.10+ and Node 24.9+. JSON inputs need only the Python standard library;
install PyYAML 6.0.3 only if you choose YAML task files.

```sh
node bin/eval.mjs l1-prepare --config /absolute/l1-config.json --output /absolute/new-l1-suite
node bin/eval.mjs l1-run --suite /absolute/new-l1-suite --output /absolute/new-l1-replies
node bin/eval.mjs l1-score --suite /absolute/new-l1-suite --output /absolute/new-l1-replies
```

L1 configuration names `store`, optional `baseRevision`, `authorModel`, a distinct
`judgeModel`, `baseUrl`, and optional frozen documents/fact queries/calibration.
The read-only `migloop-memory/1` adapter verifies snapshot/card hashes, structures,
evidence references and dependencies for `case/4`. This structural check does not
claim to reproduce a generator's semantic lint. Unsupported formats are rejected.
Each changed lesson receives three real model judgments. Uncalibrated judgments
remain review evidence; they cannot automatically promote Memory.

The host normally provides `HMBOT_MODEL_SERVICE` (a loopback Messages endpoint)
and `HMBOT_MODEL_SERVICE_TOKEN`; its model service owns credentials and durable
request budgets. Standalone use accepts an explicit endpoint credential through
`ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_API_KEY`, or `DEEPSEEK_API_KEY`. To bound standalone
calls, set `HMBOT_MEMORY_MODEL_BUDGET` to an existing JSON file such as
`{"limit":12,"used":0}`. There are no automatic HTTP retries or budget resets.

L2 uses `prepare/run/score`. `backend.py` documents the frozen task schema.
Task prompts, declared inputs and selected Memory are model-visible; checks and
correct references are grader-only. Each assertion must fail the supplied real
failure and pass its retained reference before freezing. New schema-2 suites bind
the shipped backend hashes. Legacy results remain read-only and must not be
silently rescored with a different implementation.

## Optional L3: invoke the core executor

L3 takes an independent calibrated task; it does not require an L2 suite or run.
Use `l3-prepare/l3-run/l3-score`. A configuration includes:

```json
{
  "task": "/absolute/task-with-checks.json",
  "baseline": "/absolute/historical-failure",
  "project": "/absolute/correct-reference",
  "workspace": "/absolute/model-visible-inputs",
  "memory": "/absolute/reading-pack",
  "memoryRevision": "<64-hex-revision>",
  "core": {"cli": "/absolute/core/bin/executor.mjs", "config": "/absolute/executor-runtime.json"},
  "execution": {
    "mode": "task-rerun", "harness": "dsh", "model": "deepseek-flash",
    "baseUrl": "https://api.deepseek.com/anthropic",
    "budget": {"maxRequests": 4, "timeoutSeconds": 360}
  },
  "ledger": "/absolute/persistent-l3-budget.json",
  "requestLimit": 36,
  "runs": 3
}
```

Historical replay additionally declares `sourceRef`, `sourceDigest`, `checkpoint`
and any explicit path mappings. The core owns native recordings, workspace/context
restoration, OS isolation, model relay, budgets, cancellation and raw receipts.
Plain transcripts without historical file snapshots are insufficient. Missing
capabilities fail explicitly; they are never replaced with simulated results or
silently relabeled task reruns. Configure `HMBOT_EXECUTOR_MODEL_KEY` explicitly for
real runs. Prepare/score do not call a model.

B0 has no new Memory; B1 mounts the reading pack with a retrieval instruction;
B2 injects task-linked lessons. Each runs three times. Result checks and explicit
human process review are distinct. Task rerun is not historical decision replay;
model/fidelity differences remain in receipts. Text checks do not prove compilation,
device behavior or generalization. L3 remains optional for generation and candidate
archival. Failed or incomplete execution never counts as acceptance.

## Studio console

The method console belongs to the private RSI core distribution. Use its common
launcher: `node bin/cli.mjs memory setup` then `node bin/cli.mjs memory start`.
It selects flywheel/offline inputs, generates Memory with installed components,
invokes this evaluator and archives explicit candidates to a private GitHub repo.
The evaluator package itself is independently installable and callable.
