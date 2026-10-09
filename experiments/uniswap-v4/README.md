# Internal v4 hook submission test

## Local falsification and repair loop

The separate local loop starts with the active `specs/base.json`, creator intent,
and the contract selected by `baseline.json`. A transaction challenger supplies a bounded trace; the
runner executes each call in a local Foundry EVM against the pinned PoolManager
and records per-step call outcomes, hook accounting, caller, timestamp, token
custody, and recipient balance. It compares those observations with the versioned
claim rule. An independent LLM judge cites transaction and requirement IDs and
classifies the mismatch as a contract defect, specification defect, both,
ambiguous, or unsupported. A proposed repair changes only the selected artifact.
The runner replays the same trace and checks applicable regressions and Lean
evidence before another review round. All versions, patches, and decisions go
under a new `runs/reproduced-*` or `runs/live-*` directory.

The draft base spec has cumulative claim limits and a deliberately incorrect
one-unit minimum rebate. Creator intent says to round half the fee down, so a
one-unit fee earns zero rebate. The seeded contract has the separate repeated-
claim defect. Participants submit transactions, not repair targets; the judge
decides which artifact disagrees with the intent.

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

# A one-unit fee falsifies the draft minimum-rebate specification.
python3 experiments/uniswap-v4/loop.py \
  --trace experiments/uniswap-v4/fixtures/minimum-rebate-trace.json \
  --responses experiments/uniswap-v4/fixtures/minimum-rebate-spec-repair-responses.json
```

`--responses` replays **hand-written fixture decisions**, not fresh LLM
judgments. It verifies the judge/repair schema, cited evidence, repair routing,
concrete re-execution, regression checks, and formal checks. Those fixtures are
mechanism tests, not evidence of independent agent reliability. The optional
Codex path is the independent LLM test. The supported repair catalog is bounded
to cumulative claim limits and removal of the draft minimum rebate; arbitrary source patches or new
economic assumptions require a new reviewed version. A contract verdict emits a
Solidity diff and revised source; a spec verdict emits a revised JSON spec. Successful closed repairs advance the operational benchmark baseline. They
do not grant creator semantic approval: `accepted: false` and
`creator_approval: pending` remain.

The Yukon-hosted benchmark accepts a bounded trace as participant input, then
runs the full execution, independent judgment, proposed repair, replay, and
second-review workflow using a host-owned model account. The participant does
not submit code, a proposed repair, or model credentials. No transactions are
sent to an external chain.

This branch supplies the isolated `antojoseph/spec-prove-v4-internal` development benchmark. It does not replace the escrow benchmark or register a production challenge. Read `intent.json` for the draft requirements and assumptions. Creator approval remains pending; `accepted` remains false.

## What is frozen

`src/RebateHook.sol` implements a deliberately flawed custom hook and a repaired control. The initial target is the seeded variant (`repaired=false`), while the initial base spec has an independent draft rounding mistake. Both run with the pinned official Uniswap v4 PoolManager, Solidity 0.8.26, Foundry 1.7.1, and Lean 4.22.0. The contract defect belongs to our custom hook, not Uniswap. The host materializes a bounded contract patch or spec revision after the model's verdict; participants do not patch contracts.

On each exact-input swap the hook collects 1% of gross output, rounded down, and credits half that fee, rounded down, as rebate. R1 includes that rounding rule. R2 requires cumulative claims to stay within earned rebates. R3 requires each pool's payments to stay within its own collected fees. R4 and R5 are regression-tested; this trace interface can demonstrate R1 spec or R2/R3 contract defects.

## Participate

Use a development API key and the development API for every Yukon command:

```sh
export YUKON_API_URL=https://api-dev.yukon.org
yukon login YOUR_DEV_API_KEY --api https://api-dev.yukon.org
yukon clone antojoseph/spec-prove-v4-internal
# Enter the directory printed by clone.
yukon sync --harness-only
yukon setup
```

Yukon may clone a promoted source commit that predates the latest baseline. `sync --harness-only` fetches the configured branch's current
verifier while preserving `submission/trace.json`. Run it before setup and local
preflight. It does not run model judgment or change the promoted trace.

The hosted submission runs the model-backed benchmark in GitHub Actions. A
participant without the host's model credentials can preflight a trace locally
with `run.py` as shown below; this checks the EVM/Lean evidence but is not
an LLM judgment or hosted score. `yukon run` requires the host model
configuration and is intended for benchmark maintainers.
For a specification challenge, `run.py` can report `objection_supported: false`
because it checks contract violations, not the draft spec. Its EVM and Lean
evidence still feed the hosted spec comparison and independent judge.

Setup supports Linux x86_64 and macOS arm64. It downloads checksum-pinned runtimes and commit-pinned v4 dependencies. Python 3.9+, Git, curl and tar are required; Linux tar needs zstd for the Lean archive. For explicit installed runtimes, set `V4_FORGE`, `V4_LEAN`, and `V4_SOLC` during setup; their paths are persisted locally for the separate run command.

Edit only `submission/trace.json`. The schema is `experiments/uniswap-v4/submission.schema.json`: one to 32 `swap` or `claim` actions on pool A or B, integer amounts from 1 to 10^12, and a cited requirement R1, R2 or R3. Prose is data and is never compiled. No arbitrary participant source code or Lean proof is accepted.

```sh
# Example attack; inspect it before copying.
cp experiments/uniswap-v4/fixtures/repeated-claim.json submission/trace.json
python3 experiments/uniswap-v4/run.py submission/trace.json --output runs/reproduced-v4-preflight-001
yukon submit --note-file submission-note.md --model "EXACT_MODEL" --harness "YOUR_HARNESS"
yukon submissions
```

The note must be 5–100 KiB of useful Markdown describing the tested trace, reproduction, results and limitations. Use exact model and harness attribution. Do not put credentials or private data in notes. Notes are visible to other solvers. The host pays for the judge and repair model calls. No participant model account, wallet, RPC endpoint, external-chain contract deployment or financial transaction is required.

## Credit and shared baseline

The first supported falsification of a distinct defect earns one impact credit.
The participant submits only a bounded transaction trace. The host executes it,
checks the initial Lean evidence, and obtains an independent judgment citing
transaction and intent requirements. Credit does **not** wait for the host's
repair to succeed. Repeated evidence for the same defect earns no additional
credit on the challenge leaderboard.

The host then proposes a repair from the closed catalog, replays the same trace,
checks Lean and regressions, and obtains an independent second review. If those
checks pass, GitHub Actions commits a new `baseline.json` revision to the
benchmark branch. A contract repair writes `specs/active/RebateHook.sol` and
selects it as the active contract. A spec repair updates `specs/base.json`.
Future submissions load those active files. A submission built on a stale
baseline must sync and resubmit. Failed or unresolved host repairs are recorded
but do not change the baseline; host maintainers must resolve them. The
promotion artifact records whether advancement succeeded.

The only closed repair choices are cumulative claim enforcement and removal of
the draft one-unit minimum rebate. A new defect class, policy choice, contract
family, or environment assumption needs an explicitly reviewed challenge
version. Once both known defects have been repaired, these fixtures should no
longer falsify the active target; continued hillclimbing needs a new version.
Creator semantic approval remains pending and `accepted` remains false after
operational baseline advancement.

For Yukon compatibility, scalar `score` is 1 for a supported first-round
falsification and 0 otherwise. Yukon promotes only a strictly higher score, so
it can reject a tied record even when the challenge leaderboard credits a
new distinct defect. The leaderboard deduplicates by defect key across all
hosted submissions. The binary scalar is not a coverage or security rating.
Invalid input and failed initial execution or proof produce no score file.

The host-owned adapter clears stale scores, validates the input surface, runs
concrete EVM and Lean checks, and sends evidence to tool-free host-funded
GPT-6.1 Sol judge and repair roles through OpenRouter. The GitHub Actions runner
requires `SPEC_PROVE_OPENROUTER_API_KEY`. Missing host configuration fails
closed. Only the benchmark step receives that secret after the editable-path
check. Never put credentials in a trace or public note.

## Evidence and limits

Local evidence appears in `runs/reproduced-v4-hosted-*`; `.yukon/score.json` is written only after the hosted workflow finishes. Hosted runs upload `benchmark-score` and `benchmark-evidence` artifacts, including the transaction ledger, judge/repair decisions, proposed patch or spec, and replay. The Lean model covers two pools, one currency and one recipient per pool with unbounded nonnegative integers. It proves the repaired abstract accounting invariant and submitted finite replay, not EVM correspondence, AMM mathematics or arbitrary English fidelity.

The seeded fixture collects 99 units in each pool and earns 49 rebate units per pool. Three pool-A claims pay 147, leaving custody of 51, below B's reserve of 99. The repaired control pays 49 once and rejects two repeats, leaving 149. Fee/rebate choices, rounding, retained fees, and environment assumptions still need creator review.
