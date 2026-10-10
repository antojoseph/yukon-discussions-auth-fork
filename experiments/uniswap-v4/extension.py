"""Check a model-authored candidate without treating model prose as trusted evidence."""
import json
import os
from pathlib import Path
import re
import tempfile

import run
from execution import compare, digest, execute, runtime_env


FORBIDDEN_LEAN = re.compile(r'\b(?:sorry|native_decide|axiom|unsafe|partial|opaque|run_tac|elab|macro|syntax|initialize|IO)\b|#(?:eval|reduce)\b')


def parse_proposal(proposal, current_spec, current_source, current_lean, verdict, spec_gap):
    if (not isinstance(proposal, dict) or set(proposal) !=
            {'schema_version', 'specification', 'lean_source', 'contract_source', 'reasoning'} or
            proposal.get('schema_version') != 1):
        raise ValueError('Invalid candidate proposal fields')
    for key in ('specification', 'lean_source', 'contract_source', 'reasoning'):
        value = proposal[key]
        if not isinstance(value, str) or not value.strip() or len(value) > 100000:
            raise ValueError('Invalid candidate ' + key)
    if sum(len(proposal[key]) for key in ('specification', 'lean_source', 'contract_source', 'reasoning')) > 120000:
        raise ValueError('Candidate proposal exceeds review payload budget')
    spec = json.loads(proposal['specification'], object_pairs_hook=run.no_duplicates)
    # Import lazily: loop owns the base-spec schema and also imports this module.
    import loop
    spec = loop.validate_spec(spec)
    if verdict in ('spec_defect', 'both') and spec == current_spec and proposal['lean_source'] == current_lean:
        raise ValueError('Specification repair did not change the spec or Lean model')
    if spec_gap and (spec == current_spec or proposal['lean_source'] == current_lean):
        raise ValueError('Lean coverage gap requires both a base-spec and Lean-model revision')
    if verdict in ('contract_defect', 'both') and proposal['contract_source'] == current_source:
        raise ValueError('Contract repair did not change the contract')
    if verdict == 'spec_defect' and proposal['contract_source'] != current_source:
        raise ValueError('Specification-only verdict cannot change the contract')
    if verdict == 'contract_defect' and (spec != current_spec or proposal['lean_source'] != current_lean):
        raise ValueError('Contract-only verdict cannot change the specification')
    if proposal['lean_source'] != current_lean and spec == current_spec:
        raise ValueError('Lean model changed without a base-spec revision')
    return spec, proposal['contract_source'], proposal['lean_source']


def check_lean(source, out):
    """Kernel-check the candidate model and audit its required theorem axioms."""
    out = out.resolve()
    if FORBIDDEN_LEAN.search(source) or re.search(r'^\s*import\s+(?!Std\s*$)', source, re.M):
        raise ValueError('Candidate Lean source uses an unapproved declaration or import')
    out.mkdir(parents=True, exist_ok=False)
    (out / 'Rebate.lean').write_text(source)
    audit = '\n'.join('#print axioms ' + name for name in sorted(run.THEOREMS)) + '\n'
    (out / 'Audit.lean').write_text('import Rebate\n' + audit)
    env_source = runtime_env()
    lean = env_source.get('V4_LEAN', 'lean')
    with tempfile.TemporaryDirectory(prefix='v4-lean-home-') as home:
        # Do not pass the hosted judge credential or GitHub token to model-authored Lean.
        env = {'HOME': home, 'PATH': os.path.dirname(lean) + os.pathsep + '/usr/bin:/bin',
               'LEAN_PATH': str(out)}
        code, model_log = run.command([lean, '-o', out / 'Rebate.olean', out / 'Rebate.lean'],
                                      cwd=out, env=env, timeout=120)
        (out / 'lean-model.log').write_text(model_log)
        if code:
            raise ValueError('Candidate Lean model failed to compile')
        code, audit_log = run.command([lean, out / 'Audit.lean'], cwd=out, env=env, timeout=120)
        (out / 'lean-axioms.log').write_text(audit_log)
        if code:
            raise ValueError('Candidate Lean theorem audit failed')
        run.check_axioms(audit_log, run.THEOREMS)
    return {'model_kernel_checked': True, 'model_sha256': digest(source),
            'trace_kernel_checked': False, 'contract_correspondence': 'not_proved'}


def replay_candidate(root, current_trace, history, spec, source, lean_source, out):
    """Replay every historical finding and this submission on the exact candidate."""
    import advance_baseline
    traces = advance_baseline.checked_traces(root, current_trace, history, current_is_trace=True)
    out.mkdir(parents=True, exist_ok=False)
    formal = check_lean(lean_source, out / 'lean')
    cases = []
    for index, (label, trace) in enumerate(traces):
        observed = execute(trace, True, source, out / str(index) / 'execution',
                           patched=True, run_regressions=index == 0)
        comparison = compare(spec, observed)
        if index == 0 and observed['regressions_passed'] != 10:
            raise ValueError('Candidate did not pass all Solidity regressions')
        if any(f['intent_violations'] or f.get('execution_anomalies') for f in comparison['findings']):
            raise ValueError('Candidate still violates a checked intent invariant: ' + label)
        cases.append({'label': label, 'trace_sha256': digest(trace), 'trace': trace,
                      'execution': observed, 'comparison': comparison,
                      'trace_kernel_checked': False})
    result = {'schema_version': 1, 'candidate_sha256': digest({'spec': spec, 'contract_source': source,
                                                              'lean_source': lean_source}),
              'formal': formal, 'cases': cases}
    (out / 'candidate-checks.json').write_text(json.dumps(result, indent=2) + '\n')
    return result


def validate_review(review, checks):
    expected = [case['trace_sha256'] for case in checks['cases']]
    if (not isinstance(review, dict) or set(review) !=
            {'schema_version', 'verdict', 'reviewed_candidate_sha256', 'reviewed_trace_sha256', 'reasoning', 'questions'} or
            review['schema_version'] != 1 or review['verdict'] not in ('resolved', 'still_falsified', 'ambiguous') or
            review['reviewed_candidate_sha256'] != checks['candidate_sha256'] or
            review['reviewed_trace_sha256'] != expected or
            not isinstance(review['reasoning'], str) or not review['reasoning'].strip() or
            not isinstance(review['questions'], list) or
            any(not isinstance(q, str) for q in review['questions'])):
        raise ValueError('Invalid independent candidate review')
    if review['verdict'] == 'resolved' and review['questions']:
        raise ValueError('Resolved review cannot leave interpretation questions')
    return review
