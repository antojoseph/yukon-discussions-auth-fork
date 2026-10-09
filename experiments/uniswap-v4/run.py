#!/usr/bin/env python3
"""Local-only v4 attack -> repair -> replay -> Lean evidence. No credentials or RPC."""
import argparse
import hashlib
import html
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone

BASE = Path(__file__).resolve().parent
ROOT = BASE.parents[1]
AXIOMS = {"propext", "Classical.choice", "Quot.sound"}
THEOREMS = {"Rebate.repaired_all_traces_safe", "Rebate.other_pool_unchanged",
            "Rebate.seeded_attack", "Rebate.repaired_attack", "Rebate.repaired_preserves_other_reserve"}


def no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key: " + key)
        result[key] = value
    return result


def read_submission(path):
    if path.stat().st_size > 32_768:
        raise ValueError("submission exceeds 32 KiB")
    x = json.loads(path.read_text(), object_pairs_hook=no_duplicates)
    if not isinstance(x, dict) or set(x) != {"schema_version", "family", "requirement", "description", "actions"}:
        raise ValueError("unexpected submission fields")
    if type(x["schema_version"]) is not int or x["schema_version"] != 1 or x["family"] != "uniswap-v4-rebate-v1":
        raise ValueError("wrong schema or challenge family")
    if x["requirement"] not in ("R1", "R2", "R3"):
        raise ValueError("requirement must be R1, R2 or R3")
    if not isinstance(x["description"], str) or len(x["description"]) > 2000:
        raise ValueError("invalid description")
    if not isinstance(x["actions"], list) or not 1 <= len(x["actions"]) <= 32:
        raise ValueError("provide 1..32 actions")
    for a in x["actions"]:
        if not isinstance(a, dict) or set(a) != {"op", "pool", "amount"}:
            raise ValueError("unexpected action fields")
        if a["op"] not in ("swap", "claim") or a["pool"] not in ("A", "B"):
            raise ValueError("unknown action or pool")
        if type(a["amount"]) is not int or not 1 <= a["amount"] <= 10**12:
            raise ValueError("amount must be an integer in 1..10^12")
    return x


def command(args, cwd=BASE, env=None, timeout=300):
    r = subprocess.run([str(a) for a in args], cwd=cwd, env=env, text=True,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
    return r.returncode, r.stdout


def verify_dependencies():
    lock = json.loads((BASE / "dependencies.json").read_text())
    dep = BASE / "lib/v4-core"
    code, head = command(["git", "rev-parse", "HEAD"], cwd=dep)
    if code or head.strip() != lock["v4_core_commit"]:
        raise ValueError("v4 revision mismatch; run setup.sh in a fresh checkout")
    code, status = command(["git", "status", "--porcelain", "--untracked-files=all"], cwd=dep)
    if code or status.strip():
        raise ValueError("v4 dependency has local changes")
    code, status = command(["git", "submodule", "status", "--recursive"], cwd=dep)
    if code or any(line[:1] != " " for line in status.splitlines()):
        raise ValueError("missing or mismatched dependency submodule")
    for sub, expected in lock["submodules"].items():
        code, value = command(["git", "rev-parse", "HEAD"], cwd=dep / sub)
        if code or value.strip() != expected:
            raise ValueError("submodule revision mismatch: " + sub)
    return lock


def solidity_trace(x):
    # Only validated enums and bounded integers enter code; description is never compiled.
    actions = []
    for a in x["actions"]:
        pool, n = "pool" + a["pool"], a["amount"]
        if a["op"] == "swap":
            actions.append(f'emit log_named_uint("fee_{a["pool"]}", swapIn({pool}, {n}, true));')
        else:
            actions.append(f'''{{ (bool ok,) = address(hook).call(abi.encodeCall(RebateHook.claim,
                ({pool}.toId(), currency1, {n}))); emit log_named_uint("claim_ok", ok ? 1 : 0); }}''')
    for pool in ("A", "B"):
        for field in ("collected", "paid"):
            actions.append(f'emit log_named_uint("{pool}_{field}", hook.{field}(pool{pool}.toId(), currency1));')
        actions.append(f'emit log_named_uint("{pool}_earned", hook.earned(pool{pool}.toId(), currency1, address(this)));')
    actions.append('emit log_named_uint("custody", currency1.balanceOf(address(hook)));')
    return '''// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;
import {RebateHookTest} from "./RebateHook.t.sol";
import {RebateHook} from "../src/RebateHook.sol";
import {PoolKey} from "v4-core/src/types/PoolKey.sol";
import {PoolIdLibrary} from "v4-core/src/types/PoolId.sol";
contract SubmissionReplay is RebateHookTest {
    using PoolIdLibrary for PoolKey;
    function testSubmittedSeeded() public { fixture(false); replay(); }
    function testSubmittedRepaired() public { fixture(true); replay(); }
    function replay() private {
''' + "\n".join(actions) + "\n}\n}\n"


def decode_forge(raw):
    suites = json.loads(raw)
    tests = {suite + "/" + name: value for suite, result in suites.items()
             for name, value in result.get("test_results", {}).items()}
    if not tests or any(v["status"] != "Success" for v in tests.values()):
        raise ValueError("Solidity tests failed or no tests executed")
    return tests


def replay_model(actions, logs, fixed):
    s = {p: {"collected": 0, "earned": 0, "paid": 0} for p in ("A", "B")}
    custody = 0
    events = []
    abstract = []
    cursor = 0
    for a in actions:
        p, amount = a["pool"], a["amount"]
        key, value = logs[cursor].split(": ")
        cursor += 1
        value = int(value)
        if a["op"] == "swap":
            if key != "fee_" + p or value < 0:
                raise ValueError("unexpected replay fee log")
            s[p]["collected"] += value
            s[p]["earned"] += value // 2
            custody += value
            abstract.append(f".collect .{p.lower()} {value}")
            events.append({**a, "fee": value, "succeeded": True})
        else:
            if key != "claim_ok" or value not in (0, 1):
                raise ValueError("unexpected claim result log")
            allowed = amount <= custody and (s[p]["paid"] + amount <= s[p]["earned"]
                if fixed else amount <= s[p]["earned"])
            if bool(value) != allowed:
                raise ValueError("Solidity / abstract claim result mismatch")
            if allowed:
                s[p]["paid"] += amount
                custody -= amount
            abstract.append(f".claim .{p.lower()} {amount}")
            events.append({**a, "succeeded": allowed})
    actual = dict(line.split(": ") for line in logs[cursor:])
    expected = {f"{p}_{k}": str(v) for p, values in s.items() for k, v in values.items()}
    expected["custody"] = str(custody)
    if actual != expected:
        raise ValueError("Solidity / abstract final state mismatch")
    violations = []
    if any(v["paid"] > v["earned"] for v in s.values()):
        violations.append("R2")
    if any(v["paid"] > v["collected"] for v in s.values()):
        violations.append("R3")
    return {"accounts": s, "custody": custody, "violations": violations, "events": events}, abstract


def check_axioms(log, expected):
    found = set()
    for line in log.splitlines():
        m = re.fullmatch(r"'([^']+)' (?:depends on axioms: \[(.*)\]|does not depend on any axioms)", line)
        if m:
            found.add(m.group(1))
            axioms = set(filter(None, (m.group(2) or "").split(", ")))
            if not axioms <= AXIOMS:
                raise ValueError("unexpected proof axiom: " + str(axioms - AXIOMS))
    if found != expected:
        raise ValueError("missing theorem axiom audit")


def witness_source(abstract, seeded, repaired):
    clauses = []
    for flag, result in (("false", seeded), ("true", repaired)):
        for p in ("A", "B"):
            for field, value in result["accounts"][p].items():
                clauses.append(f"(run {flag} submitted).{p.lower()}.{field} = {value}")
        clauses.append(f"(run {flag} submitted).custody = {result['custody']}")
    return ("import Rebate\nnamespace Rebate\ndef submitted : List Action := [" + ", ".join(abstract) + "]\n" +
        "theorem submitted_replay : " + " ∧\n".join(clauses) + " := by decide\n" +
        "#print axioms submitted_replay\nend Rebate\n")


def report_page(result):
    def esc(x): return html.escape(str(x))
    rows = "".join(f"<tr><td>{esc(p)}</td><td>{result['seeded']['accounts'][p]['collected']}</td>"
        f"<td>{result['seeded']['accounts'][p]['earned']}</td><td>{result['seeded']['accounts'][p]['paid']}</td>"
        f"<td>{result['repaired']['accounts'][p]['paid']}</td></tr>" for p in ("A", "B"))
    events = "".join(f"<tr><td>{i+1}</td><td>{esc(a['op'])}</td><td>{a['pool']}</td><td>{a['amount']}</td>"
        f"<td>{'succeeds' if a['succeeded'] else 'reverts'}</td><td>{'succeeds' if b['succeeded'] else 'reverts'}</td></tr>"
        for i, (a, b) in enumerate(zip(result['seeded']['events'], result['repaired']['events'])))
    return f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>spec.prove · Uniswap v4 hook demo</title><style>
body{{max-width:1000px;margin:48px auto;padding:0 24px;font:17px/1.55 system-ui;color:#18312e;background:#fafbf8}}
h1{{font-size:38px;line-height:1.15}}h2{{margin-top:34px}}.tag{{color:#4d6962}}table{{border-collapse:collapse;width:100%;background:white}}
th,td{{text-align:left;padding:10px 12px;border-bottom:1px solid #dce4de}}.box{{background:#eaf4ef;padding:18px 22px;border-radius:10px}}a{{color:#175e49}}
</style><p class="tag">spec.prove · LOCAL INTERNAL DEMO · SEEDED DEFECT</p>
<h1>A rebate claim that spends another pool’s reserves</h1>
<p>Two pools share a custom hook running against the real, pinned Uniswap v4 PoolManager. Our hook charges 1% of swap output and credits half that fee as a rebate. Rounding happens per swap.</p>
<div class="box"><strong>Intent:</strong> cumulative claims must stay within earned rebates, and each pool must preserve the other pool’s backing. The repair tracks prior claims; the fee and rebate promises stay the same.</div>
<h2>Attack → repair → replay</h2><p>{esc(result['description'])}</p>
<table><tr><th>Step</th><th>Action</th><th>Pool</th><th>Amount</th><th>Seeded hook</th><th>Repaired hook</th></tr>{events}</table>
<h2>What happened</h2><table><tr><th>Pool</th><th>Fees collected</th><th>Rebate earned</th><th>Seeded payout</th><th>Repaired payout</th></tr>{rows}</table>
<p>Shared custody after replay: <strong>{result['seeded']['custody']}</strong> seeded; <strong>{result['repaired']['custody']}</strong> repaired. Validated violations: {esc(', '.join(result['seeded']['violations']) or 'none')}.</p>
<h2>Evidence</h2><p>{result['regression_tests_passed']} Solidity regression tests passed, plus both submitted-trace replays. Lean checked the trace witness and the repaired abstract invariant for arbitrary finite action sequences. The concrete replay and accounting model agreed on all logged fees, claim outcomes, and final balances.</p>
<p><a href="evidence.json">Evidence JSON</a> · <a href="forge-regression.json">Solidity regressions</a> · <a href="forge-submission.json">Submitted replay</a> · <a href="lean-model.log">Lean model check</a> · <a href="Submitted.lean">Trace proof</a></p>
<h2>Scope and review</h2><p>This is a deliberately buggy custom hook, not a vulnerability claim about Uniswap. Lean models two pools, one currency and one recipient per pool; Solidity tests also exercise both swap directions and separate users. No theorem proves EVM correspondence. The test router is not suitable for real funds.</p>
<p>Creator approval: <strong>pending</strong>. Accepted: <strong>false</strong>. Intent and assumptions need review. No AI judge or fresh agent inference ran; this is a deterministic attack and repair demonstration. Nothing was deployed or published to Yukon.</p></html>'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("submission", nargs="?", type=Path, default=BASE / "fixtures/repeated-claim.json")
    parser.add_argument("--output", type=Path, default=ROOT / "runs" / ("reproduced-v4-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")))
    args = parser.parse_args()
    submission = read_submission(args.submission)
    lock = verify_dependencies()
    out = args.output.resolve()
    # Preserve recorded evidence and disallow clobbering prior runs.
    if out.parent != (ROOT / "runs").resolve() or not out.name.startswith("reproduced-"):
        raise ValueError("output must be a new runs/reproduced-* directory")
    out.mkdir(exist_ok=False)
    (out / "submission.json").write_text(json.dumps(submission, indent=2) + "\n")
    state = {"validation_completed": False, "accepted": False, "creator_approval": "pending",
             "contract_correspondence": "not_proved", "deployment": "none", "dependencies": lock}
    try:
        forge = os.environ.get("V4_FORGE", "forge")
        lean = os.environ.get("V4_LEAN", "lean")
        code, version = command([lean, "--version"])
        if code or not re.search(r"version 4\.22\.0(?:\s|,|\))", version):
            raise ValueError("Lean 4.22.0 required; set V4_LEAN")
        state["lean_version"] = version.strip()
        code, version = command([forge, "--version"])
        if code: raise ValueError("Forge required; set V4_FORGE")
        state["forge_version"] = version.strip()
        forge_args = [forge, "test", "--root", BASE, "--json", "-vv"]
        if os.environ.get("V4_SOLC"):
            forge_args += ["--use", str(Path(os.environ["V4_SOLC"]).resolve()), "--offline"]
        code, raw = command(forge_args + ["--match-contract", "^RebateHookTest$"])
        (out / "forge-regression.json").write_text(raw)
        if code: raise ValueError("Solidity regression run failed; inspect forge-regression.json")
        tests = decode_forge(raw)
        state["regression_tests_passed"] = len(tests)
        source = solidity_trace(submission)
        (out / "Submission.t.sol").write_text(source)
        with tempfile.NamedTemporaryFile(mode="w", dir=BASE / "test", prefix="Submission_", suffix=".t.sol", delete=False) as f:
            f.write(source)
            generated = Path(f.name)
        try:
            code, raw = command(forge_args + ["--match-path", "test/" + generated.name,
                "--match-test", "^testSubmitted(Seeded|Repaired)\\(\\)$"])
        finally:
            generated.unlink()
        (out / "forge-submission.json").write_text(raw)
        if code: raise ValueError("Submitted EVM replay failed; inspect forge-submission.json")
        tests = decode_forge(raw)
        if len(tests) != 2: raise ValueError("expected exactly two submitted replays")
        abstractions = []
        for label, fixed in (("seeded", False), ("repaired", True)):
            logs = next(v["decoded_logs"] for k, v in tests.items() if k.endswith("testSubmitted" + label.title() + "()"))
            result, abstract = replay_model(submission["actions"], logs, fixed)
            state[label] = result
            abstractions.append(abstract)
        if abstractions[0] != abstractions[1]: raise ValueError("repair changed observed fee collection")
        if state["repaired"]["violations"]: raise ValueError("repair violated the invariant")
        code, raw = command([lean, "-o", out / "Rebate.olean", BASE / "lean/Rebate.lean"])
        (out / "lean-model.log").write_text(raw)
        if code: raise ValueError("Lean model check failed")
        check_axioms(raw, THEOREMS)
        (out / "Submitted.lean").write_text(witness_source(abstractions[0], state["seeded"], state["repaired"]))
        env = {**os.environ, "LEAN_PATH": str(out)}
        code, raw = command([lean, out / "Submitted.lean"], env=env)
        (out / "lean-submission.log").write_text(raw)
        if code: raise ValueError("Lean submission witness failed")
        check_axioms(raw, {"Rebate.submitted_replay"})
        inputs = sorted([*BASE.glob("src/*.sol"), *BASE.glob("lean/*.lean"), BASE / "test/RebateHook.t.sol",
            BASE / "intent.json", BASE / "submission.schema.json", BASE / "dependencies.json", BASE / "foundry.toml", Path(__file__).resolve()])
        state["source_sha256"] = {str(p.relative_to(BASE)): hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs}
        state.update(validation_completed=True, kernel_checked=True, abstract_all_traces_invariant_proved=True,
                     concrete_abstract_replay_agree=True, description=submission["description"],
                     submitted_requirement=submission["requirement"],
                     objection_supported=submission["requirement"] in state["seeded"]["violations"])
        (out / "report.html").write_text(report_page(state))
    except Exception as exc:
        state["error"] = str(exc)
        raise
    finally:
        (out / "evidence.json").write_text(json.dumps(state, indent=2) + "\n")
    print(json.dumps({k: state[k] for k in ("validation_completed", "objection_supported", "kernel_checked", "accepted", "creator_approval")}, indent=2))
    print("Report:", out / "report.html")


if __name__ == "__main__":
    main()
