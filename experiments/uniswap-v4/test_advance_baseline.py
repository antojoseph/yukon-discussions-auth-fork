import copy
import json
import unittest

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


if __name__ == '__main__':
    unittest.main()
