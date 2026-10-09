# auto-prove: escrow specification experiment

## Later v4 falsification and repair extension

The [Uniswap v4 local experiment](experiments/uniswap-v4/README.md#local-falsification-and-repair-loop)
adds the two repair targets discussed after this escrow report. A versioned base
claim rule and exact hook source accompany every round. A challenger submits
bounded swap/claim actions, Foundry executes them against the pinned PoolManager,
and the runner records per-step accounting and token balances. An independent
Codex role is available to judge whether the evidence indicates a contract
defect, specification defect, both, ambiguity, or an unsupported objection.
The trusted repair catalog can then propose a Solidity guard patch or a new
cumulative-claim specification version. The same transactions are replayed,
followed by regression and Lean checks; parent versions and evidence remain in
the generated report.

Two saved-response mechanism runs are reproducible without model access:
the seeded contract pays 147 after earning 49 in pool A and collecting 99 there,
so the contract repair path proposes cumulative guards; a deliberately wrong
per-call draft spec predicts repeated payment while the repaired hook rejects it,
so the spec repair path proposes a revised rule. Both repaired pairs show no
mismatch on that finite trace. The saved judge decisions are hand-written test
fixtures. A fresh LLM judge run was **not completed** on this machine because
the isolated Codex CLI failed at startup. This is not evidence of independent
agent accuracy. Creator approval stays pending, `accepted` stays false, and no
general EVM correspondence theorem is claimed. The original escrow findings
below remain the recorded experiment and are unchanged.

Experiment date: 2 October 2026. This repository contains the code, synthetic creator intent, agent outputs, replay evidence and Lean source needed to reproduce the experiment.

## Question and scope

Can an independent adversarial agent find when a proposed formal specification changes a creator's smart-contract intent, and can a verifier substantiate its objection before another agent repairs the proposal?

The pilot is a fixed-token escrow. Alice funds 100 token units for Bob, with a deadline of timestamp 10. Any address may trigger release at or after that deadline. Bob receives exactly the committed amount, once. Failed transfers preserve retry eligibility, and unsolicited donations do not enlarge entitlement. Read [the complete requirements and assumptions](intent/creator.json).

Agent A proposes a structured policy plus mappings to R1–R7. Agent B receives the original intent and candidate independently, then submits concrete transaction traces. A deterministic engine compares candidate semantics with a hand-authored reference interpretation. A trusted compiler translates only closed policy choices and validated traces into Lean. Lean checks proof claims about this abstract model. Free-form agent prose is never inserted into executable Lean code.

## Recorded experiments

| Experiment | Input | Observed result |
| --- | --- | --- |
| Seeded specification review | One faithful candidate and eight developer-modified variants | All eight flawed candidates received a supported objection; faithful candidate had none |
| Objection verification | Eight concrete reviewer counterexamples | Eight supported, zero unsupported; all candidate cases checked in Lean |
| Fresh review and repair | Candidate permitting repeated payouts | Reviewer demonstrated double payment, proposer changed release rule to once, fresh reviewer found no remaining objection |
| Repaired formal target | Repaired release rule versus reference release rule | Lean checked equivalence for all configurations, states, caller classes, timestamps and transfer outcomes in the abstract model |
| Harness tests | Schema checks, replay, gating, transport and orchestration | 20 tests passed; transport unit tests use mocks, while the fresh integration run used actual inference |
| Solidity tests | Reference escrow and generated reviewer-trace replays | 19 tests passed, including one fuzz test with 256 runs and eight generated trace tests |

The actual live run used GPT-6-Sol through Codex CLI in independent ephemeral role contexts. The recorded inputs and outputs are available; fresh model output need not be identical. The eight mutated candidates are deliberately seeded defects, not mistakes spontaneously made by Agent A. This is a small pilot, not a reliability estimate.

## A concrete repair

The seeded candidate said `repeat: per_call` even though its explanatory prose described a one-release guard. The executable policy is the candidate meaning tested by the evaluator.

| Transaction | Intended behavior | Flawed candidate |
| --- | --- | --- |
| Alice funds 100 at t=1 | Escrow holds 100 | Same |
| Eve donates 100 at t=2 | Escrow holds 200; entitlement stays 100 | Same |
| Eve releases at t=10 | Bob receives 100 | Same |
| Bob releases again at t=11 | Reverts; total paid stays 100 | Pays another 100; total paid reaches 200 |

The reviewer identified an R4 violation: repeated payout exceeds the committed amount. Replay demonstrated the reachable mismatch and Lean checked its finite trace witness. Agent A then set `repeat: once` and revised its requirement mapping. Agent B's independent second review reported no objection or unresolved question. Bounded search found no remaining mismatch and Lean checked the repaired release rule's equivalence to the reference.

## Inspect the evidence

- [Nine-candidate interactive report](runs/demo/report.html) and [its JSON data](runs/demo/report.json).
- [Fresh review-and-repair interactive report](runs/validated-live-loop/report.html) and [its JSON data](runs/validated-live-loop/report.json).
- [First reviewer output](runs/validated-live-loop/round-1/review.json), [repair](runs/validated-live-loop/round-1/agent-a-repair/response.json), and [independent second review](runs/validated-live-loop/round-2/review.json).
- [First-round Lean witness](runs/validated-live-loop/round-1/lean/Candidate.lean), [repaired target proof](runs/validated-live-loop/round-2/lean/Candidate.lean), and [its checker log](runs/validated-live-loop/round-2/lean/lean-check.log).
- [Trusted Lean model](lean/Escrow.lean), [Solidity reference](contracts/src/TokenEscrow.sol), and [consolidated validation status](runs/validation.json).

GitHub displays HTML source. Clone the repository and open the HTML files locally to use the interactive reports. They need no server or model account. Runtime event streams, stderr logs, credentials and compiler binaries are excluded from the published package; structured agent responses and usage summaries are retained.

## What the result establishes

The verifier can check concrete semantic objections in this escrow policy language and formal release-rule equivalence to the trusted reference. The Lean model also proves payout-cap and beneficiary-only payout invariants for arbitrary finite action sequences. The checker permits Lean's standard propositional-extensionality axiom, `propext`, and rejects the other printed axioms. It does not use proof placeholders or unchecked native certificates.

The creator's reference interpretation is hand-authored. Lean does not determine whether arbitrary English intent was faithfully translated, and no theorem connects these abstract transitions to Solidity bytecode or general EVM behavior. Solidity tests give executable evidence for the tested cases. Creator semantic approval stays **pending** even after agent convergence.

The environment assumes exact-transfer, non-rebasing ERC20 tokens and atomic failed-transaction rollback. Lean abstracts callbacks, gas and compiler behavior; Solidity tests separately exercise a reentrant token callback. Surplus donations stay locked in v1. No contract was deployed.

## Reproduce and extend

Follow [README.md](README.md) or give [AGENTS.md](AGENTS.md) to your agent. `bash scripts/reproduce.sh` rechecks recorded evidence without fresh model inference; optional fresh-agent commands use the reader's own account.

Before publication, the complete reproduction command passed in a clean export containing only the staged repository files, with no bundled compilers or original machine paths. That check used installed Lean 4.22.0 and Solidity 0.8.28 executables and reran the 20 Python tests, 19 Solidity tests, nine seeded cases and both recorded live-loop rounds. The CI workflow repeats the same model-free checks on Ubuntu.

Reusable components include role prompts, structured submissions, counterexample adjudication, proof generation, bounded repair orchestration and the review interface. A new contract family needs a new reference interpretation, transition model and schema. A useful next experiment would measure transfer across several escrow instances and then a related family, while tracking supported objections, false criticism, cost, unresolved ambiguities and creator effort.
