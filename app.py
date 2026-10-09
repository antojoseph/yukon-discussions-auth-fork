#!/usr/bin/env python3
"""Escrow autoformalization research prototype. Python standard library only."""
from __future__ import annotations
import argparse, copy, hashlib, json, os, re, shutil, subprocess, tempfile, time
from pathlib import Path
from engine import ROOT, INTENT, validate_candidate, evaluate_review
from lean_compiler import verify


def read(path): return json.loads(Path(path).read_text())
def write(path, value):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,indent=2)+"\n")


def report(output, cases, provenance):
    output=Path(output); output.mkdir(parents=True,exist_ok=True)
    payload=dict(title=INTENT["title"], intent=INTENT, provenance=provenance, cases=cases,
                 accepted=False, human_approval="pending",
                 intent_sha256=hashlib.sha256((ROOT/"intent/creator.json").read_bytes()).hexdigest())
    write(output/"report.json",payload)
    source=(ROOT/"report.html").read_text()
    # Data stays inert and cannot close the script element or inject markup.
    encoded=json.dumps(payload).replace("<","\\u003c").replace(">","\\u003e").replace("&","\\u0026")
    (output/"report.html").write_text(source.replace("__REPORT_DATA__",encoded))
    return payload


def evaluate(candidate, review, output, skip_lean=False):
    evaluation=evaluate_review(candidate,review)
    if skip_lean: evaluation["lean"]=dict(status="not_run",kernel_checked=False)
    else: evaluation["lean"]=verify(candidate,evaluation,Path(output)/"lean")
    write(Path(output)/"candidate.json",candidate); write(Path(output)/"review.json",review)
    write(Path(output)/"evaluation.json",evaluation)
    return dict(candidate=candidate,review=review,evaluation=evaluation)


def demo(args):
    inputs=read(ROOT/"runs/adversarial-inputs.json")
    if inputs["intent"] != INTENT:
        raise ValueError("Demo input intent differs from current creator intent; re-review before replay")
    candidates=inputs["candidates"]
    reviews={r["candidate_name"]:r for r in read(ROOT/"runs/adversarial-reviews.json")["reviews"]}
    cases=[]
    for i,c in enumerate(candidates):
        cases.append(evaluate(c,reviews[c["name"]],Path(args.output)/f"case-{i+1:02d}",args.skip_lean))
    summary=report(args.output,cases,dict(
        mode="recorded_real_agents_with_developer_seeded_variants",
        proposer="Independent Agent A generated runs/revised/candidate.json from creator intent.",
        adversary="Independent Agent B reviewed the nine candidates in a separate context without evaluation code or expected answers.",
        variants="Case 01 preserves A's policy. Cases 02-09 are deliberate developer changes, not spontaneous errors from A.",
        claim="One pilot with known variants, not an estimate of general autoformalization reliability."))
    verified=sum(c["evaluation"]["validated_findings"] for c in cases)
    unsupported=sum(c["evaluation"]["unsupported_findings"] for c in cases)
    kernels=sum(c["evaluation"]["lean"]["kernel_checked"] for c in cases)
    print(f"{len(cases)} candidates, {verified} supported objections, {unsupported} unsupported objections, {kernels} Lean-checked cases")
    print(f"Report: {Path(args.output).resolve()/'report.html'}")
    print("Human semantic approval: pending. No contract has been deployed.")
    if not args.skip_lean and kernels != len(cases):
        raise RuntimeError("At least one Lean check failed; see case evaluation/log")
    return summary


def invoke_agent(role, data, output, timeout, model=None, instruction_root=None, schema_name=None):
    executable=shutil.which("codex")
    if not executable: raise RuntimeError("Codex CLI unavailable. Recorded demo needs no model access.")
    instruction_root=Path(instruction_root) if instruction_root else ROOT
    instructions=(instruction_root/"prompts"/f"{role}.txt").read_text()
    schema=instruction_root/"schemas"/(schema_name or ("candidate.json" if role=="proposer" else "review.json"))
    # Ephemeral contexts share neither conversation nor output artifacts. The
    # minimal working directory contains only this role's prompt inputs. Codex's
    # read-only sandbox still permits reads elsewhere: this is prompt isolation,
    # not an OS-level confidentiality boundary.
    with tempfile.TemporaryDirectory(prefix=f"escrow-{role}-") as isolated:
        input_path=Path(isolated)/"inputs.json"; write(input_path,data)
        local_schema=Path(isolated)/"output-schema.json"; shutil.copyfile(schema,local_schema)
        command=[executable,"exec","--ephemeral","--sandbox","read-only","--skip-git-repo-check",
                 "--disable","apps","--disable","plugins","--disable","shell_tool",
                 "--cd",isolated,"--output-schema",str(local_schema),"--color","never","--json","-"]
        if model:
            command[2:2]=["--model",model]
        # Disconnect configured MCP connectors for this model-only experiment.
        # Read names locally without emitting connector config or environment.
        listing=subprocess.run([executable,"--disable","plugins","--disable","apps","mcp","list","--json"],capture_output=True,text=True,timeout=15)
        if listing.returncode:
            raise RuntimeError("Cannot establish connector-free agent settings")
        for server in json.loads(listing.stdout):
            # CLI override keys use literal dotted segments, not quoted TOML
            # keys. Reject names that cannot safely form one segment.
            name=server["name"]
            if not re.fullmatch(r"[A-Za-z0-9_-]+",name):
                raise RuntimeError("Connector name cannot safely form a CLI override")
            command[2:2]=["-c",f'mcp_servers.{name}.enabled=false']
        prompt=instructions+"\n\nUse only the data below. Do not browse, access connectors, execute shell commands, read other files, or write files. Return the JSON directly.\n\n"+json.dumps(data)
        started=time.monotonic()
        result=subprocess.run(command,input=prompt,text=True,capture_output=True,timeout=timeout)
        output=Path(output); output.mkdir(parents=True,exist_ok=True)
        (output/"events.jsonl").write_text(result.stdout)
        (output/"stderr.log").write_text(result.stderr)
        if result.returncode:
            raise RuntimeError(f"{role} exited {result.returncode}; see {output/'stderr.log'}")
        final=None; usage=None
        for line in result.stdout.splitlines():
            try: event=json.loads(line)
            except json.JSONDecodeError: continue
            if event.get("type")=="item.completed":
                item=event.get("item",{})
                if item.get("type")=="agent_message": final=item.get("text")
            if event.get("type")=="turn.completed": usage=event.get("usage")
        if final is None: raise RuntimeError("Codex returned no final structured message")
        content=json.loads(final)
        write(output/"response.json",content)
        write(output/"usage.json",dict(elapsed_seconds=round(time.monotonic()-started,2),usage=usage,
                                      model=model or "configured_default"))
        return content


def agents(args):
    output=Path(args.output)
    model=getattr(args,"model",None)
    if output.exists(): raise ValueError("Use a new output folder to preserve run provenance")
    output.mkdir(parents=True)
    if args.rounds < 1 or args.rounds > 3: raise ValueError("Use 1-3 bounded review rounds")
    if args.candidate:
        candidate=validate_candidate(read(args.candidate))
    else:
        candidate=validate_candidate(invoke_agent("proposer",dict(intent=INTENT),output/"agent-a-initial",args.timeout,model=model))
    cases=[]
    for i in range(args.rounds):
        print(f"Round {i+1}: independent adversarial review",flush=True)
        review=invoke_agent("reviewer",dict(intent=INTENT,candidate=candidate),output/f"round-{i+1}"/"agent-b",args.timeout,model=model)
        case=evaluate(candidate,review,output/f"round-{i+1}",args.skip_lean); cases.append(case)
        e=case["evaluation"]
        report(output,cases,dict(mode="live_codex_agents", model=model or "configured_default",
                                stopping_rule="Bounded rounds or provisional convergence; human approval remains mandatory."))
        if e["unsupported_findings"]:
            print("Reviewer raised an unsupported objection. Stop for adjudication; no automatic repair.",flush=True); break
        if e["lean"]["status"] not in ("checked","not_run"):
            print("Formal check failed. Stop for inspection; no automatic acceptance.",flush=True); break
        if not e["validated_findings"] and not e["bounded_check"]["witnesses"]:
            print("No demonstrated model mismatch. Awaiting creator review.",flush=True); break
        if i+1 < args.rounds:
            candidate=validate_candidate(invoke_agent("proposer",dict(intent=INTENT,previous_candidate=candidate,
                adjudicated_objections=[f for f in e["findings"] if f["validated"]]),
                output/f"round-{i+1}"/"agent-a-repair",args.timeout,model=model))
    print(f"Report: {output.resolve()/'report.html'}")


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest="command",required=True)
    for name in ("demo","agents"):
        p=sub.add_parser(name)
        p.add_argument("--output",default=str(ROOT/"runs"/("demo" if name=="demo" else "live-"+time.strftime("%Y%m%d-%H%M%S"))))
        p.add_argument("--skip-lean",action="store_true",help="Label Lean as not run; never certify proof checks")
        if name=="agents":
            p.add_argument("--model",help="Explicit model for this run; otherwise inherit the configured default")
            p.add_argument("--candidate",help="Start with an existing candidate and test review/repair")
            p.add_argument("--rounds",type=int,default=2)
            p.add_argument("--timeout",type=int,default=240)
    e=sub.add_parser("evaluate")
    e.add_argument("candidate"); e.add_argument("review"); e.add_argument("--output",required=True)
    e.add_argument("--skip-lean",action="store_true")
    args=parser.parse_args()
    try:
        if args.command=="demo": demo(args)
        elif args.command=="agents": agents(args)
        else:
            case=evaluate(read(args.candidate),read(args.review),args.output,args.skip_lean)
            report(args.output,[case],dict(mode="imported_candidate_and_review"))
            print(f"Report: {Path(args.output).resolve()/'report.html'}")
            if not args.skip_lean and not case["evaluation"]["lean"]["kernel_checked"]:
                raise RuntimeError("Formal check failed")
    except (ValueError,RuntimeError,OSError,subprocess.TimeoutExpired) as error:
        parser.exit(1,f"Error: {error}\n")


if __name__=="__main__": main()
