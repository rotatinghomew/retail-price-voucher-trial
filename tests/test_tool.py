"""定价试验工具的端到端与复用性测试。"""
import csv
import json
import tempfile
import unittest
from pathlib import Path

from pricing_experiment.__main__ import main
from pricing_experiment.core import DataError, analyze
from pricing_experiment.decide import CONTINUE, STOP_NEW, decide

ROOT = Path(__file__).resolve().parents[1]
DRILL = ROOT / 'data' / 'drill.json'


def run_drill(out):
    return main([str(DRILL), '--out', str(out)])


class DrillRunTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out = Path(cls.tmp.name) / 'out'
        assert run_drill(cls.out) == 0
        cls.state = json.loads((cls.out / 'state.json').read_text(encoding='utf-8'))
        cls.report = (cls.out / 'report.md').read_text(encoding='utf-8')
        with open(cls.out / 'attribution_ledger.csv', encoding='utf-8') as fh:
            cls.ledger = list(csv.DictReader(fh))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_itt_keeps_non_issued_and_censoring_not_zero(self):
        # 320 个机会全部留在台账；未领券计 0；未成熟记删失而非 0
        self.assertEqual(len(self.ledger), 320)
        states = {}
        for row in self.ledger:
            states[row['value_state']] = states.get(row['value_state'], 0) + 1
        self.assertEqual(states['no_voucher'], 103)
        self.assertEqual(states['pending_refund'], 48)
        self.assertEqual(states['open_voucher'], 24)
        pending = [r for r in self.ledger if r['value_state'] == 'pending_refund']
        self.assertTrue(all(r['net_value'] == '' and r['censored'] == '1' for r in pending))

    def test_attribution_stays_with_first_assignment(self):
        # 跨店核销 31 笔，归属仍为发券店-日的组
        cross = [r for r in self.ledger if r['cross_channel'] == '1']
        self.assertEqual(len(cross), 31)
        ok = [r for r in cross if r['assignment_status'] == 'ok']
        rest = [r for r in cross if r['assignment_status'] != 'ok']
        self.assertTrue(all(r['arm'] != '' for r in ok))
        # 例外只能来自归属无法确认的重叠日
        self.assertTrue(all(r['shop'] == '南门' and r['on'] == '2026-08-04' for r in rest))
        # 重叠日（南门 2026-08-04）归属无法确认，不进比较
        conflict = [r for r in self.ledger if r['assignment_status'] == 'conflict']
        self.assertEqual(len(conflict), 10)
        self.assertTrue(all(r['on'] == '2026-08-04' and r['shop'] == '南门' for r in conflict))
        self.assertTrue(all(r['arm'] == '' for r in conflict))

    def test_net_contribution_reconciles(self):
        total = sum(int(r['net_value']) for r in self.ledger if r['net_value'] != '')
        self.assertEqual(total, 79380)  # 可归属 71970 + 重叠日 7410
        refunded = [r for r in self.ledger if r['value_state'] == 'refunded']
        self.assertTrue(all(int(r['net_value']) == -int(r['cost']) for r in refunded))

    def test_decisions_and_commitments(self):
        d = self.state['decisions']
        self.assertEqual(set(d), {'南门', '北门', '西柜', '联营'})
        for shop, st in d.items():
            self.assertEqual(st['decision'], STOP_NEW)
            self.assertTrue(any('成本' in r for r in st['reasons']))
            # 停止发新券与履行已发券并存
            self.assertEqual(len(st['honor_vouchers']), 6)
            self.assertEqual(len(st['pending_refund_receipts']), 12)
            self.assertEqual(st['revisit_on'], '2026-08-13')
            self.assertTrue(st['supports_planned_comparison'])
        # 已登记的停新行动与执行偏离都被记录，未抹掉已发券
        south = d['南门']['recorded_actions'][0]
        self.assertEqual(south['action'], '停止发新券')
        self.assertIn('28', south['execution_deviation'])

    def test_report_content(self):
        self.assertIn('仍支持预定的比较', self.report)
        self.assertIn('-99分', self.report)          # 每次机会净增益
        self.assertIn('[-516分, 318分]', self.report)  # 聚类区间
        self.assertIn('不是无效果的证据', self.report)  # 未成熟不等于无效果
        self.assertIn('停发原因是成本而非效果', self.report)
        self.assertIn('分配重叠', self.report)
        self.assertIn('S12/SX', self.report)           # 批次分配可核对
        self.assertIn('聚类稳健标准误', self.report)

    def test_deterministic_rerun(self):
        out2 = Path(self.tmp.name) / 'out2'
        self.assertEqual(run_drill(out2), 0)
        for name in ('report.md', 'state.json', 'attribution_ledger.csv'):
            self.assertEqual((self.out / name).read_bytes(), (out2 / name).read_bytes())


def rotation_fixture():
    """下一轮换商品与分配表后的同构资料：不同门店、组、日期。"""
    shops = ['东店', '西店', '南店']
    days = [f'2026-09-0{i}' for i in range(1, 7)]
    alloc, opps, vouchers, receipts = [], [], [], []
    for shop in shops:
        for j, day in enumerate(days):
            arm = '甲价' if j % 2 == 0 else '乙价'
            alloc.append(dict(id=f'{shop}-{day}', shop=shop, on=day, arm=arm,
                              declared_on='2026-08-30'))
    # 东店 09-02 分配重叠（组别不一致）；西店 09-06 分配缺失；南店两日重叠
    alloc.append(dict(id='东店-09-02-X', shop='东店', on='2026-09-02', arm='甲价',
                      declared_on='2026-08-30'))
    alloc.append(dict(id='南店-09-02-X', shop='南店', on='2026-09-02', arm='甲价',
                      declared_on='2026-08-30'))
    alloc.append(dict(id='南店-09-04-X', shop='南店', on='2026-09-04', arm='甲价',
                      declared_on='2026-08-30'))
    alloc = [a for a in alloc if not (a['id'] == '西店-2026-09-06')]
    vid = rid = 0
    for shop in shops:
        for j, day in enumerate(days):
            for k in range(4):
                oid = f'{shop}-{day}-{k}'
                take = k in (1, 2)
                vid += 1
                v = None
                if take:
                    v = f'W{vid}'
                    arm = '甲价' if j % 2 == 0 else '乙价'
                    vouchers.append(dict(id=v, exposure=oid, holder=f'H{vid}',
                                         shop=shop, issued_on=day,
                                         expires_on=f'2026-09-0{j + 3}',
                                         quoted=3000 if arm == '甲价' else 2500))
                    if k == 1:
                        rid += 1
                        rday = f'2026-09-0{min(j + 2, 6)}'
                        receipts.append(dict(
                            id=f'R{rid}', voucher=v,
                            shop=shops[(shops.index(shop) + 1) % 3],
                            redeemed_on=rday, paid=vouchers[-1]['quoted'],
                            refund_on='2026-09-05' if j == 0 else None,
                            cost=1200))
                opps.append(dict(id=oid, visitor=f'H{vid}', shop=shop, on=day,
                                 eligible=True, voucher=v if take else None))
    return {
        'allocation': alloc, 'opportunities': opps, 'vouchers': vouchers,
        'receipts': receipts, 'interruptions': [],
        'settings': [dict(through='2026-09-06', refund_days=2,
                          min_gain=100, max_redemption_cost=5000)],
        'actions': [],
    }


class RotationReuseTest(unittest.TestCase):
    """换一批门店、商品与分配表后，同一能力仍可用。"""

    @classmethod
    def setUpClass(cls):
        cls.data = rotation_fixture()
        cls.result = analyze(cls.data)
        cls.decisions = decide(cls.result)

    def test_attribution_categories(self):
        ig = self.result.integrity
        self.assertEqual(ig['eligible'], 72)
        # 东店 1 天 + 南店 2 天重叠（各 4 机会），西店 1 天缺分配（4 机会）
        self.assertEqual(ig['excluded_by_status'].get('conflict'), 12)
        self.assertEqual(ig['excluded_by_status'].get('missing_allocation'), 4)
        self.assertTrue(ig['supported'])  # 整体排除占比 16/72 < 1/3

    def test_shop_level_support(self):
        # 南店 8/24 = 1/3 的机会归属无法确认：不支持比较，只能维持试验
        self.assertFalse(self.decisions['南店']['supports_comparison'])
        self.assertEqual(self.decisions['南店']['decision'], CONTINUE)
        self.assertTrue(self.decisions['东店']['supports_comparison'])

    def test_effect_and_censoring(self):
        eff = self.result.effects['乙价']
        self.assertIsNotNone(eff['se_cluster'])
        self.assertGreater(eff['clusters'], 2)
        # 最后一天核销的观察期到 09-08 > 09-06：删失存在且不计零
        cens = sum(st['censored'] for st in self.result.arm_stats.values())
        self.assertGreater(cens, 0)

    def test_baseline_override(self):
        result = analyze(self.data, baseline='乙价')
        self.assertIn('甲价', result.effects)

    def test_cross_channel_kept_with_issuer(self):
        cross = self.result.deviations['cross_channel']
        self.assertEqual(len(cross), len(self.data['receipts']))
        self.assertTrue(all(c['issued_shop'] != c['redeemed_shop'] for c in cross))

    def test_validation_hard_error(self):
        bad = json.loads(json.dumps(self.data))
        bad['receipts'][0]['paid'] = '3000'
        with self.assertRaises(DataError):
            analyze(bad)
        with self.assertRaises(DataError):
            analyze({k: v for k, v in self.data.items() if k != 'vouchers'})


if __name__ == '__main__':
    unittest.main()
