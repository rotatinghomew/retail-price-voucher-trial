"""核查练习数据的原始事实，不计算分析结果。"""
from pathlib import Path
import json
import unittest


class DrillDataTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = Path(__file__).resolve().parents[1] / 'data' / 'drill.json'
        cls.data = json.loads(source.read_text(encoding='utf-8'))

    def test_schema(self):
        expected = {'allocation': ('id', 'shop', 'on', 'arm', 'declared_on'), 'opportunities': ('id', 'visitor', 'shop', 'on', 'eligible', 'voucher'), 'vouchers': ('id', 'exposure', 'holder', 'shop', 'issued_on', 'expires_on', 'quoted'), 'receipts': ('id', 'voucher', 'shop', 'redeemed_on', 'paid', 'refund_on', 'cost'), 'interruptions': ('shop', 'on', 'reason'), 'settings': ('through', 'refund_days', 'min_gain', 'max_redemption_cost'), 'actions': ('shop', 'action', 'starts', 'confirmed_on')}
        self.assertEqual(set(self.data), set(expected))
        for name, fields in expected.items():
            with self.subTest(table=name):
                self.assertTrue(self.data[name])
                self.assertTrue(all(set(row) == set(fields) for row in self.data[name]))

    def test_stop_policy_has_prior_voucher_activity(self):
        action = self.data['actions'][0]
        cards = {row['id']: row for row in self.data['vouchers']}
        self.assertTrue(any(cards[row['voucher']]['shop'] != row['shop'] for row in self.data['receipts']))
        self.assertTrue(any(card['shop'] == action['shop'] and card['issued_on'] < action['starts'] <= card['expires_on'] for card in cards.values()))
        keys = [(row['shop'], row['on']) for row in self.data['allocation']]
        self.assertLess(len(set(keys)), len(keys))
        self.assertTrue(any(row['eligible'] and row['voucher'] is None for row in self.data['opportunities']))
