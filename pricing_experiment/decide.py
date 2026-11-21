"""决策层：把效果证据、兑现成本界线与归属完整性译成门店级处置。

三类处置：可以维持试验 / 必须停止发新券 / 可以扩大使用。
判定顺序：归属完整性 -> 兑现成本上限 -> 效果与最小有意义增益。
停止发新券不等于否认已发券：已发未兑的券继续按发券时报价兑现，
未到观察期的交易陆续回来后，复跑本工具会更新判断；
历史报价与历史处置不随新数据改写。
"""
from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

from .core import _parse_day

CONTINUE = '维持试验'
STOP_NEW = '停止发新券'
SCALE_UP = '扩大使用'

# 效果证据不足时不下“无效”结论的最低成熟样本要求（每组完整机会数）
MIN_COMPLETE_PER_ARM = 30


def _shop_scope(result, shop):
    recs = [r for r in result.records if r.eligible and r.shop == shop]
    lost = [r for r in recs if not r.attributable]
    days_per_arm = defaultdict(set)
    for r in recs:
        if r.attributable:
            days_per_arm[r.assignment.arm].add(r.on)
    share = len(lost) / len(recs) if recs else 0.0
    supported = (bool(days_per_arm)
                 and all(len(v) >= 2 for v in days_per_arm.values())
                 and len(days_per_arm) >= 2 and share < 1 / 3)
    return recs, lost, share, supported


def _effect_verdict(result):
    """整体效果证据（门店级样本太小，显著性以整体聚类区间为准）。"""
    out = {}
    for arm, eff in result.effects.items():
        if eff is None or eff['ci_lo'] is None:
            out[arm] = ('unknown', eff)
            continue
        mg = result.settings.min_gain
        thin = (result.arm_stats[arm]['complete'] < MIN_COMPLETE_PER_ARM
                or result.arm_stats[result.baseline]['complete'] < MIN_COMPLETE_PER_ARM)
        if mg is None:
            out[arm] = ('unknown', eff)
        elif thin or (eff['ci_lo'] <= mg <= eff['ci_hi']):
            # 区间仍覆盖最小有意义增益：不能当作无效果
            out[arm] = ('inconclusive', eff)
        elif eff['ci_lo'] > mg:
            out[arm] = ('gain', eff)
        else:
            out[arm] = ('below_gain', eff)
    return out


def decide(result):
    shops = sorted({r.shop for r in result.records})
    settings = result.settings
    verdicts = _effect_verdict(result)
    decisions = {}
    for shop in shops:
        recs, lost, share, supported = _shop_scope(result, shop)
        reasons = []
        cost = result.deviations['cost'].get(shop)
        cost_breach = (cost is not None and cost['limit'] is not None
                       and cost['latest_cost'] > cost['limit'])
        if not supported:
            decision = CONTINUE
            reasons.append(
                f'归属无法确认的机会占 {share:.0%}（{len(lost)}/{len(recs)}），'
                '需先修复分配重叠或错位，暂不能据此比较')
        elif cost_breach:
            decision = STOP_NEW
            reasons.append(
                f'单位兑现成本升至 {cost["latest_cost"]}分，'
                f'超过上限 {cost["limit"]}分（自 {cost["first_breach_day"]} 起超限，'
                f'累计 {cost["n_breach"]} 笔）')
        else:
            vlist = [v for v in verdicts.values()]
            for arm, (verdict, eff) in verdicts.items():
                if verdict == 'gain':
                    reasons.append(
                        f'{arm} 相对 {result.baseline} 的每次机会净增益 '
                        f'{eff["diff"]:.0f}分，95%区间 [{eff["ci_lo"]:.0f}, {eff["ci_hi"]:.0f}] '
                        f'整体高于最小有意义增益 {settings.min_gain}分')
                elif verdict == 'below_gain':
                    reasons.append(
                        f'{arm} 相对 {result.baseline} 的每次机会净增益 '
                        f'{eff["diff"]:.0f}分，95%区间上限 {eff["ci_hi"]:.0f}分 '
                        f'仍低于最小有意义增益 {settings.min_gain}分')
                elif verdict == 'inconclusive':
                    reasons.append(
                        f'{arm} 相对 {result.baseline} 的每次机会净增益 '
                        f'{eff["diff"]:.0f}分，95%区间 [{eff["ci_lo"]:.0f}, {eff["ci_hi"]:.0f}] '
                        f'仍覆盖最小有意义增益 {settings.min_gain}分，'
                        '证据不足，不能视为无效')
                else:
                    reasons.append(f'{arm} 的效果证据不足，无法判定')
            if any(v == 'below_gain' for v, _ in vlist):
                decision = STOP_NEW
            elif vlist and all(v == 'gain' for v, _ in vlist):
                decision = SCALE_UP
            else:
                decision = CONTINUE
        # 效果注记：成本原因停发时也要说明效果证据状态，避免误读为“无效”
        for arm, (verdict, eff) in verdicts.items():
            if cost_breach and verdict == 'inconclusive':
                reasons.append(
                    f'效果维度证据不足（{arm} 增益区间 '
                    f'[{eff["ci_lo"]:.0f}, {eff["ci_hi"]:.0f}]分），停发原因是成本而非效果')
        decisions[shop] = dict(
            decision=decision, reasons=reasons,
            supports_comparison=supported,
            excluded_share=share,
        )
    return decisions


def build_state(result, decisions, actions):
    """供下期门店安排使用的状态：处置、未了承诺、复看日期。"""
    settings = result.settings
    through = settings.through
    shops = sorted({r.shop for r in result.records})
    state_shops = {}
    for shop in shops:
        recs = [r for r in result.records if r.eligible and r.shop == shop]
        honor = sorted(r.voucher['id'] for r in recs
                       if r.state == 'open_voucher')
        pending = sorted(r.receipt['id'] for r in recs
                         if r.state == 'pending_refund')
        revisit = None
        marks = []
        for r in recs:
            if r.state == 'pending_refund':
                marks.append(_parse_day(r.receipt['redeemed_on'], r.receipt['id'])
                             + timedelta(days=settings.refund_days))
            elif r.state == 'open_voucher':
                marks.append(_parse_day(r.voucher['expires_on'], r.voucher['id']))
        if marks:
            revisit = max(marks).isoformat()
        recorded = []
        for a in actions:
            if a['shop'] != shop:
                continue
            entry = dict(action=a['action'], starts=a['starts'],
                         confirmed_on=a['confirmed_on'])
            for dev in result.deviations['issue_after_stop']:
                if dev['shop'] == shop:
                    entry['execution_deviation'] = (
                        f'生效后仍发券 {dev["vouchers_issued"]} 张')
            recorded.append(entry)
        state_shops[shop] = dict(
            decision=decisions[shop]['decision'],
            reasons=decisions[shop]['reasons'],
            supports_planned_comparison=decisions[shop]['supports_comparison'],
            honor_vouchers=honor,
            pending_refund_receipts=pending,
            revisit_on=revisit,
            recorded_actions=recorded,
            note='已发未兑券在有效期内按发券时报价兑现；历史报价不追溯调整',
        )
    return {
        'tool': 'pricing_experiment',
        'as_of': through.isoformat(),
        'settings': dict(through=through.isoformat(),
                         refund_days=settings.refund_days,
                         min_gain=settings.min_gain,
                         max_redemption_cost=settings.max_redemption_cost),
        'baseline': result.baseline,
        'arms': result.arms,
        'supports_planned_comparison': result.integrity['supported'],
        'decisions': state_shops,
        'immutable_quote_ledger_sha256': result.quote_hash,
        'quote_ledger_rows': result.quote_rows,
        'source_sha256': result.source_hash,
        'notes': [
            '本状态由资料快照生成；未到观察期的交易成熟后复跑本工具更新判断。',
            '报价台账指纹对应发券时点事实，只增不改；处置只对下期安排生效。',
        ],
    }
