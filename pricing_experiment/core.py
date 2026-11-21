"""核心口径：加载校验、归属、净贡献、删失与聚类稳健推断。

业务口径（与 README 一致，改动需评审）：
- 分配单位是门店内自然日；同日多条分配即冲突，不是两个独立试验。
  冲突各条若组别一致，组仍可知但记偏离；组别不一致则归属无法确认。
- 合资格机会无论是否领券都留在试验中（ITT），不因未得券回填剔除。
- 券的成交与撤单归首次有效分配（发券门店-日期），不随收银处当前组移动。
- 撤单退回该笔实收；净贡献 = 实收净额 - 单位可变成本，撤单笔 = -单位成本
  （成本随履约发生，资料未记载追回，按沉没处理）。
- 缺少撤单日期不代表观察期结束：核销后 refund_days 内仍可能撤单，
  该笔记未成熟（删失）而不是按零计入；未核销且未过有效期的券同理。
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

SCHEMA = {
    'allocation': ('id', 'shop', 'on', 'arm', 'declared_on'),
    'opportunities': ('id', 'visitor', 'shop', 'on', 'eligible', 'voucher'),
    'vouchers': ('id', 'exposure', 'holder', 'shop', 'issued_on', 'expires_on', 'quoted'),
    'receipts': ('id', 'voucher', 'shop', 'redeemed_on', 'paid', 'refund_on', 'cost'),
    'interruptions': ('shop', 'on', 'reason'),
    'settings': ('through', 'refund_days', 'min_gain', 'max_redemption_cost'),
    'actions': ('shop', 'action', 'starts', 'confirmed_on'),
}

# 机会取值状态
ST_NO_VOUCHER = 'no_voucher'            # 未发券：最终，计 0
ST_EXPIRED = 'expired_unredeemed'       # 券已过期未核销：最终，计 0
ST_OPEN_VOUCHER = 'open_voucher'        # 券未核销且仍在有效期：删失
ST_KEPT = 'kept'                        # 核销且观察期满未撤单：最终，计 paid-cost
ST_REFUNDED = 'refunded'                # 已撤单：最终，计 -cost
ST_PENDING = 'pending_refund'           # 核销但撤单观察期未结束：删失

# 归属状态
A_OK = 'ok'                             # 唯一有效分配
A_SAME = 'conflict_same_arm'            # 同日多条分配但组别一致（记偏离，组仍可知）
A_CONFLICT = 'conflict'                 # 同日多条分配且组别不一致：归属无法确认
A_MISSING = 'missing_allocation'        # 无分配记录
A_BROKEN = 'broken_reference'           # 券/单引用断裂

CENSORED_STATES = (ST_OPEN_VOUCHER, ST_PENDING)


class DataError(Exception):
    """资料结构或事实层面的硬错误。"""


@dataclass(frozen=True)
class Settings:
    through: date
    refund_days: int
    min_gain: int | None
    max_redemption_cost: int | None


@dataclass(frozen=True)
class Assignment:
    status: str
    arm: str | None
    allocation_ids: tuple


@dataclass
class OppRecord:
    """一个合资格机会的归属与取值。"""
    opp_id: str
    visitor: str
    shop: str
    on: str
    eligible: bool
    assignment: Assignment
    voucher: dict | None
    receipt: dict | None
    state: str
    value: int | None          # 成熟口径下的净贡献；删失为 None
    bound_lo: int | None       # 删失时的取值下界
    bound_hi: int | None       # 删失时的取值上界
    cross_channel: bool

    @property
    def cluster(self):
        return (self.shop, self.on)

    @property
    def attributable(self):
        return self.assignment.status in (A_OK, A_SAME)


def load_data(path):
    text = Path(path).read_text(encoding='utf-8')
    return json.loads(text), hashlib.sha256(text.encode('utf-8')).hexdigest()


def _parse_day(raw, where):
    try:
        return date.fromisoformat(raw)
    except (TypeError, ValueError):
        raise DataError(f'{where}: 日期无法解析: {raw!r}')


def validate(data):
    """结构硬校验；返回 Settings。引用断裂不在这里拦，归入归属无法确认。"""
    errors = []
    if not isinstance(data, dict):
        raise DataError('顶层必须是对象（表名 -> 行数组）')
    for name, fields in SCHEMA.items():
        if name not in data:
            errors.append(f'缺少表 {name}')
            continue
        rows = data[name]
        if not isinstance(rows, list):
            errors.append(f'表 {name} 不是数组')
            continue
        # 事实主表不能为空；事件表（停发、行动、券、单）允许为空
        if name in ('allocation', 'opportunities', 'settings') and not rows:
            errors.append(f'表 {name} 为空')
            continue
        for i, row in enumerate(rows):
            if not isinstance(row, dict) or set(row) != set(fields):
                errors.append(f'表 {name} 第 {i} 行字段应为 {sorted(fields)!r}，实际 {sorted(row)!r}')
                break
    if errors:
        raise DataError('；'.join(errors))

    for name, key in (('allocation', 'id'), ('opportunities', 'id'),
                      ('vouchers', 'id'), ('receipts', 'id')):
        ids = [r['id'] for r in data[name]]
        if len(set(ids)) != len(ids):
            errors.append(f'表 {name} 存在重复编号')
    for name, money_fields in (('vouchers', ('quoted',)),
                               ('receipts', ('paid', 'cost'))):
        for r in data[name]:
            for f in money_fields:
                if not isinstance(r[f], int):
                    errors.append(f'表 {name} {r["id"]} 的 {f} 不是整数分')
    if errors:
        raise DataError('；'.join(errors))

    s = data['settings'][0]
    settings = Settings(
        through=_parse_day(s['through'], 'settings.through'),
        refund_days=int(s['refund_days']),
        min_gain=s.get('min_gain'),
        max_redemption_cost=s.get('max_redemption_cost'),
    )
    for r in data['allocation']:
        _parse_day(r['on'], f'allocation {r["id"]}')
    for r in data['vouchers']:
        _parse_day(r['issued_on'], f'voucher {r["id"]}')
        _parse_day(r['expires_on'], f'voucher {r["id"]}')
    for r in data['receipts']:
        _parse_day(r['redeemed_on'], f'receipt {r["id"]}')
        if r['refund_on'] is not None:
            _parse_day(r['refund_on'], f'receipt {r["id"]}')
    return settings


def build_assignments(allocation):
    """(shop, on) -> Assignment。同日多条即冲突。"""
    by_day = defaultdict(list)
    for row in allocation:
        by_day[(row['shop'], row['on'])].append(row)
    out = {}
    for key, rows in by_day.items():
        ids = tuple(sorted(r['id'] for r in rows))
        arms = {r['arm'] for r in rows}
        if len(rows) == 1:
            out[key] = Assignment(A_OK, rows[0]['arm'], ids)
        elif len(arms) == 1:
            out[key] = Assignment(A_SAME, rows[0]['arm'], ids)
        else:
            out[key] = Assignment(A_CONFLICT, None, ids)
    return out


def attribute(data, settings):
    """把每个机会落到分配与净贡献；返回 (records, orphans)。"""
    assignments = build_assignments(data['allocation'])
    vouchers = {v['id']: v for v in data['vouchers']}
    opps = {o['id']: o for o in data['opportunities']}
    receipts_by_voucher = defaultdict(list)
    orphans = {'receipts': [], 'vouchers': []}
    for r in data['receipts']:
        receipts_by_voucher[r['voucher']].append(r)
    for vid in receipts_by_voucher:
        if vid not in vouchers:
            orphans['receipts'].append(vid)
    for v in data['vouchers']:
        if v['exposure'] not in opps:
            orphans['vouchers'].append(v['id'])

    costs = [r['cost'] for r in data['receipts']]
    cost_lo = min(costs) if costs else 0
    cost_hi = max(costs) if costs else 0

    records = []
    for o in data['opportunities']:
        key = (o['shop'], o['on'])
        assignment = assignments.get(key, Assignment(A_MISSING, None, ()))
        voucher = None
        receipt = None
        state = ST_NO_VOUCHER
        value, lo, hi = 0, None, None
        cross = False
        if not o['eligible']:
            state = 'not_eligible'
            value = None
        elif o['voucher'] is not None:
            voucher = vouchers.get(o['voucher'])
            if voucher is None or voucher['exposure'] != o['id']:
                assignment = Assignment(A_BROKEN, None, assignment.allocation_ids)
                state = ST_OPEN_VOUCHER
                value = None
            else:
                recs = receipts_by_voucher.get(voucher['id'], [])
                receipt = recs[0] if recs else None
                if receipt is not None:
                    cross = receipt['shop'] != voucher['shop']
                    if receipt['refund_on'] is not None:
                        state, value = ST_REFUNDED, -receipt['cost']
                    else:
                        matured = (_parse_day(receipt['redeemed_on'], receipt['id'])
                                   + timedelta(days=settings.refund_days) <= settings.through)
                        if matured:
                            state, value = ST_KEPT, receipt['paid'] - receipt['cost']
                        else:
                            state, value = ST_PENDING, None
                            lo, hi = -receipt['cost'], receipt['paid'] - receipt['cost']
                elif voucher['expires_on'] <= settings.through.isoformat():
                    state, value = ST_EXPIRED, 0
                else:
                    # 未核销且仍有效：可能不再核销（0），也可能核销后撤单（-成本）
                    state, value = ST_OPEN_VOUCHER, None
                    lo, hi = -cost_hi, max(voucher['quoted'] - cost_lo, 0)
        records.append(OppRecord(
            opp_id=o['id'], visitor=o['visitor'], shop=o['shop'], on=o['on'],
            eligible=bool(o['eligible']), assignment=assignment,
            voucher=voucher, receipt=receipt, state=state, value=value,
            bound_lo=lo, bound_hi=hi, cross_channel=cross,
        ))
    return records, orphans


# ---------------------------------------------------------------- 指标

def _new_stat():
    return dict(opps=0, complete=0, censored=0, net=0,
                bound_lo=0, bound_hi=0, issued=0, redeemed=0,
                mature_receipts=0, refunds=0, cross_channel=0,
                quoted_sum=0, quoted_n=0)


def _add(st, rec):
    st['opps'] += 1
    if rec.voucher is not None:
        st['issued'] += 1
        st['quoted_sum'] += rec.voucher['quoted']
        st['quoted_n'] += 1
    if rec.receipt is not None:
        st['redeemed'] += 1
        if rec.cross_channel:
            st['cross_channel'] += 1
    if rec.state in CENSORED_STATES:
        st['censored'] += 1
        st['bound_lo'] += rec.bound_lo
        st['bound_hi'] += rec.bound_hi
    elif rec.value is not None:
        st['complete'] += 1
        st['net'] += rec.value
        if rec.receipt is not None:
            st['mature_receipts'] += 1
            if rec.state == ST_REFUNDED:
                st['refunds'] += 1


def _finish(st):
    st = dict(st)
    st['per_opp'] = st['net'] / st['complete'] if st['complete'] else None
    tot = st['complete'] + st['censored']
    if tot:
        st['per_opp_lo'] = (st['net'] + st['bound_lo']) / tot
        st['per_opp_hi'] = (st['net'] + st['bound_hi']) / tot
    else:
        st['per_opp_lo'] = st['per_opp_hi'] = None
    st['mean_quoted'] = st['quoted_sum'] / st['quoted_n'] if st['quoted_n'] else None
    return st


def summarize(records):
    """按组、门店×组、门店×日汇总（仅归属可确认的机会）。"""
    usable = [r for r in records if r.eligible and r.attributable]
    by_arm = defaultdict(_new_stat)
    by_shop_arm = defaultdict(_new_stat)
    by_shop_day = defaultdict(_new_stat)
    for r in usable:
        arm = r.assignment.arm
        _add(by_arm[arm], r)
        _add(by_shop_arm[(r.shop, arm)], r)
        _add(by_shop_day[(r.shop, r.on, arm)], r)
    return ({k: _finish(v) for k, v in by_arm.items()},
            {k: _finish(v) for k, v in by_shop_arm.items()},
            {k: _finish(v) for k, v in by_shop_day.items()})


def cluster_effect(records, baseline, arm):
    """每次机会净贡献的组间差（arm - baseline），门店-日聚类 CR1 稳健标准误。

    返回 None 表示无法估计（某组无成熟观测或聚类不足）。
    """
    obs = [(r.cluster, 1.0 if r.assignment.arm == arm else 0.0, r.value)
           for r in records
           if r.eligible and r.attributable
           and r.assignment.arm in (baseline, arm) and r.value is not None]
    n = len(obs)
    n_t = sum(1 for _, t, _ in obs if t == 1.0)
    if n_t == 0 or n_t == n:
        return None
    clusters = defaultdict(list)
    for cl, t, v in obs:
        clusters[cl].append((t, v))
    G = len(clusters)
    mean_b = sum(v for _, t, v in obs if t == 0.0) / (n - n_t)
    mean_t = sum(v for _, t, v in obs if t == 1.0) / n_t
    diff = mean_t - mean_b
    var_b = sum((v - mean_b) ** 2 for _, t, v in obs if t == 0.0)
    var_t = sum((v - mean_t) ** 2 for _, t, v in obs if t == 1.0)
    se_naive = math.sqrt(var_b / (n - n_t - 1) / (n - n_t) + var_t / (n_t - 1) / n_t) \
        if n_t > 1 and n - n_t > 1 else None
    if G < 2:
        return dict(baseline=baseline, arm=arm, n=n, n_base=n - n_t, n_arm=n_t,
                    clusters=G, mean_base=mean_b, mean_arm=mean_t, diff=diff,
                    se_cluster=None, se_naive=se_naive, ci_lo=None, ci_hi=None)
    # 两回归元（截距+组哑变量）OLS 的聚类三明治，CR1 小样本校正
    sxx = [[n, n_t], [n_t, n_t]]
    det = sxx[0][0] * sxx[1][1] - sxx[0][1] ** 2
    inv = [[sxx[1][1] / det, -sxx[0][1] / det], [-sxx[0][1] / det, sxx[0][0] / det]]
    b0 = mean_b
    meat = [[0.0, 0.0], [0.0, 0.0]]
    for cl, rows in clusters.items():
        res = [(x, v - (b0 + diff * x)) for x, v in rows]
        s = (sum(u for _, u in res), sum(x * u for x, u in res))
        for i in range(2):
            for j in range(2):
                meat[i][j] += s[i] * s[j]
    cr1 = (G / (G - 1)) * ((n - 1) / (n - 2))
    var = [[cr1 * sum(inv[i][a] * meat[a][b] * inv[b][j]
                      for a in range(2) for b in range(2))
            for j in range(2)] for i in range(2)]
    se = math.sqrt(max(var[1][1], 0.0))
    return dict(baseline=baseline, arm=arm, n=n, n_base=n - n_t, n_arm=n_t,
                clusters=G, mean_base=mean_b, mean_arm=mean_t, diff=diff,
                se_cluster=se, se_naive=se_naive,
                ci_lo=diff - 1.96 * se, ci_hi=diff + 1.96 * se)


def day_level(records):
    """日历日×组的成熟口径均值（敏感性视图；组与日期共线时仅供描述）。"""
    cell = defaultdict(lambda: [0, 0])
    for r in records:
        if r.eligible and r.attributable and r.value is not None:
            cell[(r.on, r.assignment.arm)][0] += r.value
            cell[(r.on, r.assignment.arm)][1] += 1
    return {k: (s / c, c) for k, (s, c) in sorted(cell.items())}


# ---------------------------------------------------------------- 偏离与完整性

def find_deviations(data, records, settings):
    dev = {}
    vouchers = {v['id']: v for v in data['vouchers']}

    # 报价偏离：同组报价的众数为基准价，偏离者列出
    mode_price = {}
    by_arm_quoted = defaultdict(list)
    for r in records:
        if r.attributable and r.voucher is not None:
            by_arm_quoted[r.assignment.arm].append(r.voucher['quoted'])
    for arm, qs in by_arm_quoted.items():
        mode_price[arm] = max(set(qs), key=qs.count)
    dev['quote'] = [dict(voucher=r.voucher['id'], shop=r.shop, on=r.on,
                         arm=r.assignment.arm, quoted=r.voucher['quoted'],
                         expected=mode_price[r.assignment.arm])
                    for r in records
                    if r.attributable and r.voucher is not None
                    and r.voucher['quoted'] != mode_price[r.assignment.arm]]
    dev['arm_price'] = mode_price

    # 兑现偏离：实收与报价不符、过期核销
    dev['paid_mismatch'] = [dict(receipt=r.receipt['id'], voucher=r.voucher['id'],
                                 paid=r.receipt['paid'], quoted=r.voucher['quoted'])
                            for r in records if r.receipt is not None
                            and r.receipt['paid'] != r.voucher['quoted']]
    dev['expired_redemption'] = [dict(receipt=r.receipt['id'], voucher=r.voucher['id'],
                                      redeemed_on=r.receipt['redeemed_on'],
                                      expires_on=r.voucher['expires_on'])
                                 for r in records if r.receipt is not None
                                 and r.receipt['redeemed_on'] > r.voucher['expires_on']]

    # 跨渠道：收银行与发券店不同（允许，但归属不随收银移动）
    dev['cross_channel'] = [dict(receipt=r.receipt['id'], voucher=r.voucher['id'],
                                 issued_shop=r.voucher['shop'], redeemed_shop=r.receipt['shop'],
                                 arm=r.assignment.arm if r.attributable else None)
                            for r in records if r.cross_channel]

    # 发券偏离：停发日仍发券、停新行动生效后仍发券、冲突日发券
    issued_by_day = defaultdict(int)
    for r in records:
        if r.voucher is not None:
            issued_by_day[(r.shop, r.on)] += 1
    dev['interruptions'] = []
    for i in data['interruptions']:
        n = issued_by_day.get((i['shop'], i['on']), 0)
        # 仅停发类事由要求当日零发券；其余（如分配重叠）只记录事实
        expects_stop = '停' in i['reason']
        dev['interruptions'].append(dict(
            shop=i['shop'], on=i['on'], reason=i['reason'], issued=n,
            expects_stop=expects_stop,
            compliant=(n == 0) if expects_stop else None))
    dev['issue_after_stop'] = []
    for a in data['actions']:
        if '停止' not in a['action']:
            continue
        n = sum(1 for r in records
                if r.voucher is not None and r.shop == a['shop']
                and r.voucher['issued_on'] >= a['starts'])
        if n:
            dev['issue_after_stop'].append(dict(shop=a['shop'], action=a['action'],
                                                starts=a['starts'], vouchers_issued=n))
    conflict_days = {(r.shop, r.on) for r in records
                     if r.assignment.status == A_CONFLICT}
    dev['issue_on_conflict_day'] = [dict(shop=k[0], on=k[1], issued=v)
                                    for k, v in sorted(issued_by_day.items())
                                    if k in conflict_days]

    # 兑现成本：最新单位成本与超限事实
    cost_by_shop = defaultdict(list)
    for row in data['receipts']:
        v = vouchers.get(row['voucher'])
        if v is not None:
            cost_by_shop[v['shop']].append((row['redeemed_on'], row['cost'], row['id']))
    cost = {}
    limit = settings.max_redemption_cost
    for shop, rows in cost_by_shop.items():
        latest_day = max(d for d, _, _ in rows)
        latest = max(c for d, c, _ in rows if d == latest_day)
        breaches = sorted({d for d, c, _ in rows if limit is not None and c > limit})
        cost[shop] = dict(latest_cost=latest, latest_day=latest_day,
                          n_breach=sum(1 for _, c, _ in rows if limit is not None and c > limit),
                          first_breach_day=breaches[0] if breaches else None,
                          limit=limit)
    dev['cost'] = cost
    return dev


def integrity_summary(data, records, orphans):
    eligible = [r for r in records if r.eligible]
    lost = [r for r in eligible if not r.attributable]
    by_status = defaultdict(list)
    for r in lost:
        by_status[r.assignment.status].append(r)
    conflict_days = sorted({(r.shop, r.on) for r in by_status.get(A_CONFLICT, [])}
                           | {(r.shop, r.on) for r in by_status.get(A_SAME, [])})
    missing_days = sorted({(r.shop, r.on) for r in by_status.get(A_MISSING, [])})
    # 每个有效组至少两个有效门店-日，且排除占比低于三分之一，才认定仍支持预定比较
    days_per_arm = defaultdict(set)
    for r in eligible:
        if r.attributable:
            days_per_arm[r.assignment.arm].add((r.shop, r.on))
    share = len(lost) / len(eligible) if eligible else 0.0
    supported = (bool(days_per_arm) and all(len(v) >= 2 for v in days_per_arm.values())
                 and len(days_per_arm) >= 2 and share < 1 / 3)
    return dict(
        eligible=len(eligible),
        excluded=len(lost),
        excluded_share=share,
        excluded_by_status={k: len(v) for k, v in by_status.items()},
        conflict_days=[dict(shop=s, on=o) for s, o in conflict_days],
        missing_days=[dict(shop=s, on=o) for s, o in missing_days],
        orphan_receipts=orphans['receipts'],
        orphan_vouchers=orphans['vouchers'],
        days_per_arm={k: len(v) for k, v in sorted(days_per_arm.items())},
        supported=supported,
    )


def quote_ledger_hash(records):
    """发券时点的报价台账指纹：历史报价只增不改，供复跑核对。"""
    rows = sorted((r.voucher['id'], r.shop, r.on,
                   r.assignment.arm or '', r.voucher['quoted'])
                  for r in records if r.voucher is not None)
    payload = json.dumps(rows, ensure_ascii=False).encode('utf-8')
    return hashlib.sha256(payload).hexdigest(), len(rows)


@dataclass
class Result:
    settings: Settings
    records: list
    arms: list
    baseline: str
    arm_stats: dict
    shop_arm_stats: dict
    shop_day_stats: dict
    effects: dict                 # arm -> cluster_effect 结果
    day_cells: dict
    deviations: dict
    integrity: dict
    quote_hash: str
    quote_rows: int
    source_hash: str


def analyze(data, source_hash='', baseline=None):
    settings = validate(data)
    records, orphans = attribute(data, settings)
    arms = sorted({r.assignment.arm for r in records if r.attributable})
    if not arms:
        raise DataError('没有任何可归属的机会，无法比较')
    if baseline is None:
        first_day = min(r.on for r in records if r.attributable)
        day_arms = sorted({r.assignment.arm for r in records
                           if r.attributable and r.on == first_day})
        baseline = day_arms[0]
    if baseline not in arms:
        raise DataError(f'基准组 {baseline!r} 不在可归属组 {arms!r} 中')
    arm_stats, shop_arm_stats, shop_day_stats = summarize(records)
    effects = {a: cluster_effect(records, baseline, a) for a in arms if a != baseline}
    deviations = find_deviations(data, records, settings)
    integrity = integrity_summary(data, records, orphans)
    qhash, qrows = quote_ledger_hash(records)
    return Result(settings=settings, records=records, arms=arms, baseline=baseline,
                  arm_stats=arm_stats, shop_arm_stats=shop_arm_stats,
                  shop_day_stats=shop_day_stats, effects=effects,
                  day_cells=day_level(records), deviations=deviations,
                  integrity=integrity, quote_hash=qhash, quote_rows=qrows,
                  source_hash=source_hash)
