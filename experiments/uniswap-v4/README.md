# Internal v4 hook submission test

## Local falsification and repair loop

The separate local loop starts with `specs/base.json`, creator intent, and the
seeded hook source. A transaction challenger supplies a bounded trace; the
runner executes each call in a local Foundry EVM against the pinned PoolManager
and records per-step call outcomes, hook accounting, caller, timestamp, token
custody, and recipient balance. It compares those observations with the versioned
claim rule. An independent LLM judge cites transaction and requirement IDs and
classifies the mismatch as a contract defect, specification defect, both,
ambiguous, or unsupported. A proposed repair changes only the selected artifact.
The runner replays the same trace and checks applicable regressions and Lean
evidence before another review round. All versions, patches, and decisions go
under a new `runs/reproduced-*` or `runs/live-*` directory.

With your authenticated Codex CLI account, a fresh judge/proposer run is:

```sh
python3 experiments/uniswap-v4/loop.py --trace experiments/uniswap-v4/fixtures/repeated-claim.json
```

Omit `--trace` to let a separate challenger propose one. These commands send
the synthetic intent, source, trace, and replay evidence to your configured model
provider; no model account is needed for the saved-response examples below.
Use an available model with `--model MODEL` if needed. Role contexts are ephemeral,
read-only, tool-disabled, and connector-disabled; this is context separation,
not an operating-system confidentiality boundary. If the Codex CLI cannot start
with those controls, the live inference check is **not run**.

```sh
# Executed counterexample leads to a proposed Solidity guard change.
python3 experiments/uniswap-v4/loop.py \
  --trace experiments/uniswap-v4/fixtures/repeated-claim.json \
  --responses experiments/uniswap-v4/fixtures/contract-repair-responses.json

# A deliberately wrong per-call draft spec leads to a proposed spec revision.
python3 experiments/uniswap-v4/loop.py \
  --spec experiments/uniswap-v4/specs/per-call-draft.json --contract repaired \
  --trace experiments/uniswap-v4/fixtures/repeated-claim.json \
  --responses experiments/uniswap-v4/fixtures/spec-repair-responses.json
```

`--responses` replays **hand-written fixture decisions**, not fresh LLM
judgments. It verifies the judge/repair schema, cited evidence, repair routing,
concrete re-execution, regression checks, and formal checks. Those fixtures are
mechanism tests, not evidence of independent agent reliability. The optional
Codex path is the independent LLM test. The supported repair catalog is bounded
to cumulative claim limits for this v4 family; arbitrary source patches or new
economic assumptions require a new reviewed version. A contract verdict emits a
Solidity diff and revised source; a spec verdict emits a revised JSON spec. Both
remain proposals with `accepted: false` and `creator_approval: pending`.

The Yukon-hosted benchmark accepts a bounded trace as participant input, then
runs the full execution, independent judgment, proposed repair, replay, and
second-review workflow using a host-owned model account. The participant does
not submit code, a proposed repair, or model credentials. No transactions are
sent to an external chain.

This branch supplies the isolated `antojoseph/spec-prove-v4-internal` development benchmark. It does not replace the escrow benchmark or register a production challenge. Read `intent.json` for the draft requirements and assumptions. Creator approval remains pending; `accepted` remains false.

## What is frozen

`src/RebateHook.sol` implements a deliberately flawed custom hook and a repaired control. The target is the seeded variant (`repaired=false`). Both run with the pinned official Uniswap v4 PoolManager, Solidity 0.8.26, Foundry 1.7.1, and Lean 4.22.0. The defect belongs to our custom hook, not Uniswap. The host materializes a bounded contract patch or spec revision after the model's verdict; participants do not patch contracts.

On each exact-input swap the hook collects 1% of gross output, rounded down, and credits half that fee, rounded down, as rebate. R2 requires cumulative claims to stay within earned rebates. R3 requires each pool's payments to stay within its own collected fees. Requirements R1, R4 and R5 are regression-tested; this trace interface scores only R2 and R3.

## Participate

Use a development API key and the development API for every Yukon command:

```sh
export YUKON_API_URL=https://api-dev.yukon.org
yukon login YOUR_DEV_API_KEY --api https://api-dev.yukon.org
yukon clone antojoseph/spec-prove-v4-internal
# Enter the directory printed by clone.
yukon setup
```

The hosted submission runs the model-backed benchmark in GitHub Actions. A
participant without the host's model credentials can preflight a trace locally
with `run.py` as shown below; this checks the EVM/Lean evidence but is not
an LLM judgment or hosted score. `yukon run` requires the host model
configuration and is intended for benchmark maintainers.

Setup supports Linux x86_64 and macOS arm64. It downloads checksum-pinned runtimes and commit-pinned v4 dependencies. Python 3.9+, Git, curl and tar are required; Linux tar needs zstd for the Lean archive. For explicit installed runtimes, set `V4_FORGE`, `V4_LEAN`, and `V4_SOLC` during setup; their paths are persisted locally for the separate run command.

Edit only `submission/trace.json`. The schema is `experiments/uniswap-v4/submission.schema.json`: one to 32 `swap` or `claim` actions on pool A or B, integer amounts from 1 to 10^12, and a claimed requirement R2 or R3. Prose is data and is never compiled. No arbitrary participant source code or Lean proof is accepted.

```sh
# Example attack; inspect it before copying.
cp experiments/uniswap-v4/fixtures/repeated-claim.json submission/trace.json
python3 experiments/uniswap-v4/run.py submission/trace.json --output runs/reproduced-v4-preflight-001
yukon submit --note-file submission-note.md --model "EXACT_MODEL" --harness "YOUR_HARNESS"
yukon submissions
```

The note must be 5–100 KiB of useful Markdown describing the tested trace, reproduction, results and limitations. Use exact model and harness attribution. Do not put credentials or private data in notes. Notes are visible to other solvers. The host pays for the judge and repair model calls. No participant model account, wallet, RPC endpoint, external-chain contract deployment or financial transaction is required.

## Score and validation

The non-violating baseline scores **0**. A supported submitted objection earns one point for each independently demonstrated requirement in the first concrete replay: R2 and R3, **maximum 2**, only if the independent judge identifies the contract defect, the trusted patch passes same-trace replay and regressions, the second review finds no remaining mismatch, and Lean checks pass. The provided repeated-claim fixture is expected to score **2** when those model judgments succeed. Unsupported claims and non-violating traces score 0. Invalid input, failed execution or proof, and unsuccessful repair verification produce **no score file**. This small ceiling is intentional for an internal end-to-end mechanism test, not a competitive research benchmark.

The trusted adapter clears stale scores first, validates the input surface, runs the concrete EVM and Lean checks, sends evidence to tool-free host-funded GPT-6.1 Sol judge and repair roles through OpenRouter, applies only a closed validated edit, and repeats the EVM and Lean checks. The GitHub Actions runner requires the repository secret `SPEC_PROVE_OPENROUTER_API_KEY`. Missing host configuration fails closed with no score. Only the benchmark step receives the secret, after the editable-path check. Never put credentials in `submission/trace.json` or a public note. A promoted Yukon submission records the trace and hosted score; it does not approve a specification or contract.

Scoring is evidence coverage, not contract acceptance. `accepted: false`, `creator_approval: pending`, and `contract_correspondence: not_proved` remain separate from Yukon's submission promotion status.

## Evidence and limits

Local evidence appears in `runs/reproduced-v4-hosted-*`; `.yukon/score.json` is written only after the hosted workflow finishes. Hosted runs upload `benchmark-score` and `benchmark-evidence` artifacts, including the transaction ledger, judge/repair decisions, proposed patch or spec, and replay. The Lean model covers two pools, one currency and one recipient per pool with unbounded nonnegative integers. It proves the repaired abstract accounting invariant and submitted finite replay, not EVM correspondence, AMM mathematics or arbitrary English fidelity.

The seeded fixture collects 99 units in each pool and earns 49 rebate units per pool. Three pool-A claims pay 147, leaving custody of 51, below B's reserve of 99. The repaired control pays 49 once and rejects two repeats, leaving 149. Fee/rebate choices, rounding, retained fees, and environment assumptions still need creator review.
