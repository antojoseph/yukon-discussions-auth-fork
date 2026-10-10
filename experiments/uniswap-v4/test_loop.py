import copy
import json
from pathlib import Path
import tempfile
import unittest
import loop
from execution import compare, patch_contract


class RepairRoutingTests(unittest.TestCase):
    def setUp(self):
        self.spec = loop.validate_spec({**json.loads((loop.run.BASE / 'specs/base.json').read_text()),
                                        'minimum_rebate': 1})
        self.source = (loop.run.BASE / 'src/RebateHook.sol').read_text()
        self.findings = {'findings': [{'id': 'tx-4'}, {'id': 'tx-5'}]}
        self.judge = json.loads((loop.run.BASE / 'fixtures/contract-repair-responses.json').read_text())[0]['judge']

    def test_judge_cannot_cite_unobserved_or_unmapped_violation(self):
        j = copy.deepcopy(self.judge)
        j['evidence_ids'] = ['tx-999']
        with self.assertRaisesRegex(ValueError, 'not executed'):
            loop.validate_judgment(j, self.findings)
        j = copy.deepcopy(self.judge)
        j['requirement_ids'] = []
        with self.assertRaisesRegex(ValueError, 'citations'):
            loop.validate_judgment(j, self.findings)

    def test_repair_cannot_change_other_artifact_or_noop(self):
        p = {'schema_version': 1, 'target': 'spec', 'change': 'cumulative', 'reasoning': 'Change policy'}
        with self.assertRaisesRegex(ValueError, 'target'):
            loop.validate_repair(p, self.judge, self.spec, 'seeded')
        j = {**self.judge, 'verdict': 'spec_defect'}
        with self.assertRaisesRegex(ValueError, 'Unsupported repair'):
            loop.validate_repair(p, j, self.spec, 'repaired')

    def test_minimum_rebate_spec_repair_is_bounded(self):
        judgment = {**self.judge, 'verdict': 'spec_defect', 'requirement_ids': ['R1']}
        proposal = {'schema_version': 1, 'target': 'spec', 'change': 'remove_minimum_rebate',
                    'reasoning': 'A one-unit fee earns zero rebate under the creator intent.'}
        self.assertEqual(loop.validate_repair(proposal, judgment, self.spec, 'seeded'), proposal)
        with self.assertRaisesRegex(ValueError, 'Unsupported repair'):
            loop.validate_repair(proposal, judgment, {**self.spec, 'minimum_rebate': 0}, 'seeded')

    def test_contract_patch_removes_seeded_per_call_guard(self):
        proposed = patch_contract(self.source)
        self.assertNotIn('if (amount > entitlement) revert InvalidClaim();', proposed)
        self.assertIn('if (amount > entitlement - claimed[pool][currency][msg.sender])', proposed)
        self.assertIn('if (amount > collected[pool][currency] - paid[pool][currency])', proposed)

    def test_bounded_raw_call_is_data_and_rejects_source_injection(self):
        trace = {'schema_version': 1, 'family': 'uniswap-v4-rebate-v1', 'requirement': 'R4',
                 'description': 'Probe direct hook access',
                 'actions': [{'op': 'call', 'target': 'hook', 'caller': 'bob', 'calldata': '0x1234'}]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'trace.json'
            path.write_text(json.dumps(trace))
            parsed = loop.run.read_submission(path)
            source = loop.run.solidity_trace(parsed)
            self.assertIn('vm.prank(BOB)', source)
            self.assertIn('address(hook).call(hex"1234")', source)
            trace['actions'][0]['calldata'] = '0x1234"); selfdestruct(payable(BOB)); //'
            path.write_text(json.dumps(trace))
            with self.assertRaisesRegex(ValueError, 'invalid bounded local call'):
                loop.run.read_submission(path)

    def test_comparison_detects_spec_defect_with_no_intent_violation(self):
        observed = {'transactions': [
            {'id': 'tx-1', 'action': {'op': 'swap', 'pool': 'A', 'amount': 10000}, 'succeeded': True, 'fee': 100,
             'after': {'A_collected': 100, 'A_earned': 50, 'A_paid': 0, 'B_collected': 0, 'B_earned': 0, 'B_paid': 0, 'custody': 100}},
            {'id': 'tx-2', 'action': {'op': 'claim', 'pool': 'A', 'amount': 50}, 'succeeded': True, 'fee': None,
             'after': {'A_collected': 100, 'A_earned': 50, 'A_paid': 50, 'B_collected': 0, 'B_earned': 0, 'B_paid': 0, 'custody': 50}},
            {'id': 'tx-3', 'action': {'op': 'claim', 'pool': 'A', 'amount': 50}, 'succeeded': False, 'fee': None,
             'after': {'A_collected': 100, 'A_earned': 50, 'A_paid': 50, 'B_collected': 0, 'B_earned': 0, 'B_paid': 0, 'custody': 50}},
        ]}
        differences = compare({'claim_limit': 'per_call'}, observed)['findings']
        self.assertEqual(differences[0]['id'], 'tx-3')
        self.assertEqual(differences[0]['intent_violations'], [])
        self.assertIn('call_outcome', differences[0]['spec_mismatches'])

    def test_one_unit_fee_falsifies_minimum_rebate_draft(self):
        observed = {'transactions': [{'id': 'tx-1', 'action': {'op': 'swap', 'pool': 'A', 'amount': 200},
                    'succeeded': True, 'fee': 1,
                    'after': {'A_collected': 1, 'A_earned': 0, 'A_paid': 0,
                              'B_collected': 0, 'B_earned': 0, 'B_paid': 0, 'custody': 1}}]}
        findings = compare(self.spec, observed)['findings']
        self.assertEqual(findings[0]['id'], 'tx-1')
        self.assertEqual(findings[0]['intent_violations'], [])
        self.assertIn('A_earned', findings[0]['spec_mismatches'])
        self.assertEqual(compare({**self.spec, 'minimum_rebate': 0}, observed)['findings'], [])


if __name__ == '__main__':
    unittest.main()
