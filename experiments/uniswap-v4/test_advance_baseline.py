import copy
import contextlib
import hashlib
import io
import json
import shutil
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

import advance_baseline
import loop
from execution import digest, patch_contract


class BaselineAdvanceTests(unittest.TestCase):
    def setUp(self):
        self.base = loop.run.BASE
        self.baseline = {'schema_version': 1, 'revision': 0, 'contract': 'seeded',
                         'contract_source': 'src/RebateHook.sol'}
        self.spec = {**json.loads((self.base / 'specs/base.json').read_text()), 'minimum_rebate': 1}
        self.source = (self.base / 'src/RebateHook.sol').read_text()
        self.intent_hash = digest(json.loads((self.base / 'intent.json').read_text()))

    def report(self, family):
        responses = json.loads((self.base / 'fixtures' / (family + '-repair-responses.json')).read_text())
        findings = ([{'id': 'tx-1', 'intent_violations': [], 'spec_mismatches': ['A_earned']}]
                    if family == 'minimum-rebate-spec' else
                    [{'id': 'tx-4', 'intent_violations': ['R2'], 'spec_mismatches': []},
                     {'id': 'tx-5', 'intent_violations': ['R2', 'R3'], 'spec_mismatches': []}])
        next_spec = {**self.spec, 'minimum_rebate': 0} if family == 'minimum-rebate-spec' else self.spec
        next_source = self.source if family == 'minimum-rebate-spec' else patch_contract(self.source)
        next_contract = 'seeded' if family == 'minimum-rebate-spec' else 'patched'
        first_version = digest(dict(spec=self.spec, contract='seeded', source=self.source,
                                    intent_sha256=self.intent_hash))
        return {
            'status': 'no_demonstrated_mismatch', 'baseline_revision': 0, 'intent_sha256': self.intent_hash,
            'rounds': [
                {'version': first_version,
                 'spec': self.spec, 'contract': 'seeded', 'status': 'repair_replayed_pending_review',
                 'verification': {'kernel_checked': True}, 'comparison': {'findings': findings},
                 'judgment': responses[0]['judge'], 'repair': responses[0]['repair'],
                 'repair_recheck': {'kernel_checked': True, 'comparison': {'findings': []}}},
                {'parent_version': first_version,
                 'version': digest(dict(spec=next_spec, contract=next_contract, source=next_source,
                                        intent_sha256=self.intent_hash)),
                 'spec': next_spec, 'contract': next_contract,
                 'verification': {'kernel_checked': True}, 'comparison': {'findings': []},
                 'judgment': responses[1]['judge']},
            ],
        }

    def test_contract_repair_becomes_next_active_contract(self):
        next_baseline, spec, source = advance_baseline.next_version(
            self.report('contract'), self.baseline, self.spec, self.source)
        self.assertEqual(next_baseline['revision'], 1)
        self.assertEqual(next_baseline['contract'], 'patched')
        self.assertEqual(spec, self.spec)
        self.assertIn('entitlement - claimed[pool][currency][msg.sender]', source)

    def test_spec_repair_becomes_next_base_spec(self):
        next_baseline, spec, source = advance_baseline.next_version(
            self.report('minimum-rebate-spec'), self.baseline, self.spec, self.source)
        self.assertEqual(next_baseline['revision'], 1)
        self.assertEqual(next_baseline['contract'], 'seeded')
        self.assertEqual(spec['minimum_rebate'], 0)
        self.assertIsNone(source)

    def test_stale_baseline_and_failed_review_cannot_advance(self):
        report = self.report('contract')
        stale = {**self.baseline, 'revision': 1}
        with self.assertRaisesRegex(ValueError, 'current baseline'):
            advance_baseline.next_version(report, stale, self.spec, self.source)
        report = copy.deepcopy(report)
        report['rounds'][1]['judgment']['verdict'] = 'contract_defect'
        with self.assertRaisesRegex(ValueError, 'second review'):
            advance_baseline.next_version(report, self.baseline, self.spec, self.source)

    def test_raw_evm_evidence_can_advance_without_claiming_a_lean_trace_proof(self):
        report = self.report('contract')
        first, second = report['rounds']
        first['execution'] = {'transactions': [{'action': {'op': 'call'}}]}
        first['verification'] = {'kernel_checked': False, 'evm_executed': True}
        first['repair_recheck'] = {'kernel_checked': False,
                                   'execution': {'execution': 'local_foundry_evm_calls'},
                                   'comparison': {'findings': []}}
        second['verification'] = {'kernel_checked': False, 'evm_executed': True}
        updated, _, _ = advance_baseline.next_version(report, self.baseline, self.spec, self.source)
        self.assertEqual(updated['revision'], 1)

    def test_extension_promotion_binds_exact_files_and_every_trace(self):
        root = self.base
        baseline = json.loads((root / 'baseline.json').read_text())
        spec = json.loads((root / 'specs/base.json').read_text())
        source = (root / baseline['contract_source']).read_text()
        lean_source = (root / 'lean/Rebate.lean').read_text()
        revised_spec = {**spec, 'schema_version': 2,
                        'additional_requirements': ['A caller-sensitive raw action has defined behavior.']}
        revised_lean = lean_source + '\n-- Proposed additional coverage.\n'
        proposal = {'schema_version': 1, 'specification': json.dumps(revised_spec),
                    'contract_source': source, 'lean_source': revised_lean,
                    'reasoning': 'Describe the missing behavior.'}
        history = json.loads((root / 'specs/falsification-history.json').read_text())
        trace_path = root / 'fixtures/repeated-claim.json'
        traces = advance_baseline.checked_traces(root, trace_path, history, current_is_trace=True)
        hashes = [digest(trace) for _, trace in traces]
        candidate_hash = digest({'spec': revised_spec, 'contract_source': source,
                                 'lean_source': revised_lean})
        judgment = {'schema_version': 1, 'verdict': 'spec_defect', 'evidence_ids': ['tx-1'],
                    'requirement_ids': ['R4'], 'defect_key': 'spec:caller_sensitive_rule',
                    'spec_gap': True, 'reasoning': 'The model omits caller-sensitive behavior.',
                    'questions': []}
        report = {'status': 'extension_verified', 'baseline_revision': baseline['revision'],
                  'accepted': False, 'creator_approval': 'pending',
                  'contract_correspondence': 'not_proved',
                  'judge_mode': 'hosted_openrouter_tool_free_roles',
                  'intent_sha256': self.intent_hash,
                  'rounds': [{'version': digest(dict(spec=spec, contract=baseline['contract'],
                                                     source=source, intent_sha256=self.intent_hash)),
                              'spec': spec, 'contract': baseline['contract'], 'status': 'extension_verified',
                              'verification': {'evm_executed': True},
                              'execution': {'source_sha256': hashlib.sha256(source.encode()).hexdigest(),
                                            'transactions': [{'id': 'tx-1', 'action': {'op': 'call'}}]},
                              'comparison': {'findings': []}, 'judgment': judgment,
                              'spec_extension_proposal': proposal,
                              'extension_recheck': {'candidate_sha256': candidate_hash,
                                                    'formal': {'model_kernel_checked': True},
                                                    'cases': [{'trace_sha256': h} for h in hashes]},
                              'extension_reviews': [
                                  {'schema_version': 1, 'verdict': 'resolved',
                                   'reviewed_candidate_sha256': candidate_hash,
                                   'reviewed_trace_sha256': [h],
                                   'reasoning': 'This cited trace is addressed.', 'questions': []}
                                  for h in hashes]}]}
        updated, changed_spec, changed_source, changed_lean = advance_baseline.next_extension_version(
            report, baseline, spec, source, lean_source, history, trace_path, root)
        self.assertEqual(updated['revision'], baseline['revision'] + 1)
        self.assertEqual(changed_spec, revised_spec)
        self.assertEqual(changed_source, source)
        self.assertEqual(changed_lean, revised_lean)
        report['rounds'][0]['extension_recheck']['candidate_sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'bind the proposed files'):
            advance_baseline.next_extension_version(report, baseline, spec, source,
                                                    lean_source, history, trace_path, root)
        report['rounds'][0]['extension_recheck']['candidate_sha256'] = candidate_hash
        report['rounds'][0]['extension_reviews'].pop()
        with self.assertRaisesRegex(ValueError, 'every trace'):
            advance_baseline.next_extension_version(report, baseline, spec, source,
                                                    lean_source, history, trace_path, root)

    def test_credited_trace_is_recorded_when_host_repair_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / 'experiments/uniswap-v4'
            (root / 'specs/active').mkdir(parents=True)
            shutil.copytree(self.base / 'fixtures', root / 'fixtures')
            for name in ('baseline.json',):
                shutil.copyfile(self.base / name, root / name)
            for name in ('specs/base.json', 'specs/falsification-history.json',
                         'specs/active/RebateHook.sol'):
                shutil.copyfile(self.base / name, root / name)
            report_dir = base / 'report'
            (report_dir / 'round-1').mkdir(parents=True)
            trace_path = report_dir / 'round-1/trace.json'
            shutil.copyfile(self.base / 'fixtures/repeated-claim.json', trace_path)
            trace = json.loads(trace_path.read_text())
            trace['description'] = 'Synthetic credited history case.'
            trace_path.write_text(json.dumps(trace))
            key = 'spec:novel_caller_rule'
            report = {'status': 'repair_failed_replay', 'accepted': False,
                      'creator_approval': 'pending', 'contract_correspondence': 'not_proved',
                      'rounds': [{'verification': {'evm_executed': True},
                                  'execution': {'transactions': [{'id': 'tx-1'}]},
                                  'comparison': {'findings': []},
                                  'judgment': {'schema_version': 1, 'verdict': 'spec_defect',
                                               'evidence_ids': ['tx-1'], 'requirement_ids': [trace['requirement']],
                                               'defect_key': key, 'spec_gap': True,
                                               'reasoning': 'Synthetic finding for history persistence.',
                                               'questions': []}}]}
            report_path = report_dir / 'report.json'
            report_path.write_text(json.dumps(report))
            score_path = base / 'score.json'
            score_path.write_text(json.dumps({'score': 1, 'metrics': {
                'verified_falsification': 1, 'repair_target': 'spec', 'defect_key': key,
                'host_repair_verified': False}}))
            result_path = base / 'result.json'
            original_history = (root / 'specs/falsification-history.json').read_text()
            with patch('sys.argv', ['advance_baseline.py', '--report', str(report_path), '--base', str(base),
                                    '--score', str(score_path), '--result', str(result_path)]):
                with contextlib.redirect_stdout(io.StringIO()):
                    advance_baseline.main()
            result = json.loads(result_path.read_text())
            self.assertEqual(result['status'], 'finding_recorded')
            self.assertEqual(result['progress'], 'stalled')
            progress = json.loads((root / 'specs/progress.json').read_text())
            self.assertEqual(progress['state'], 'stalled')
            self.assertEqual(progress['finding_sha256'], digest(trace))
            self.assertEqual(json.loads((root / 'baseline.json').read_text())['revision'], 2)
            entries = json.loads((root / 'specs/falsification-history.json').read_text())['traces']
            self.assertIn(digest(trace), {entry['sha256'] for entry in entries})
            (root / 'specs/falsification-history.json').write_text(original_history)
            report['status'] = 'no_demonstrated_mismatch'
            report_path.write_text(json.dumps(report))
            score = json.loads(score_path.read_text())
            score['metrics']['host_repair_verified'] = True
            score_path.write_text(json.dumps(score))
            with patch('sys.argv', ['advance_baseline.py', '--report', str(report_path), '--base', str(base),
                                    '--score', str(score_path), '--result', str(result_path)]):
                with contextlib.redirect_stdout(io.StringIO()):
                    advance_baseline.main()
            result = json.loads(result_path.read_text())
            self.assertEqual(result['status'], 'finding_recorded')
            self.assertIsNotNone(result['promotion_error'])
            self.assertEqual(json.loads((root / 'specs/progress.json').read_text())['state'], 'stalled')
            self.assertIn(digest(trace), {entry['sha256'] for entry in
                json.loads((root / 'specs/falsification-history.json').read_text())['traces']})
            repaired_baseline = {**json.loads((root / 'baseline.json').read_text()), 'revision': 3}
            with patch('advance_baseline.promote_candidate', return_value=(
                    repaired_baseline, json.loads((root / 'specs/base.json').read_text()),
                    (root / 'specs/active/RebateHook.sol').read_text(), None)):
                with patch('sys.argv', ['advance_baseline.py', '--report', str(report_path), '--base', str(base),
                                        '--score', str(score_path), '--result', str(result_path)]):
                    with contextlib.redirect_stdout(io.StringIO()):
                        advance_baseline.main()
            self.assertEqual(json.loads(result_path.read_text())['status'], 'baseline_updated')
            self.assertEqual(json.loads((root / 'specs/progress.json').read_text())['state'], 'active')
            self.assertEqual(json.loads((root / 'specs/progress.json').read_text())['baseline_revision'], 3)


if __name__ == '__main__':
    unittest.main()
