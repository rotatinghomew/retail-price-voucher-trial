"""输出层：Markdown 报告、门店核对台账 CSV、状态 JSON。"""
from __future__ import annotations

import csv
import json
from collections import defaultdict

from .core import CENSORED_STATES, ST_REFUNDED


def fen(x):
    return '—' if x is None else f'{x:.0f}分'


def pct(x):
    return '—' if x is None else f'{x:.1%}'


def _row(cells):
    return '| ' + ' | '.join(str(c) for c in cells) + ' |'


def render_markdown(result, decisions, state):
    s = result.settings
    ig = result.integrity
    out = []
    w = out.append

    w('# 分店定价试验报告')
    w('')
    w(f'- 观察截止（as_of）：{s.through.isoformat()}；撤单观察期 {s.refund_days} 天')
    w(f'- 最小有意义增益：{fen(s.min_gain)} / 每次机会；'
      f'兑现单位成本上限：{fen(s.max_redemption_cost)}')
    w(f'- 基准组：{result.baseline}；比较组：{"、".join(a for a in result.arms if a != result.baseline)}')
    w(f'- 报价台账指纹 `{state["immutable_quote_ledger_sha256"][:12]}…`'
      f'（{state["quote_ledger_rows"]} 张券，发券时点事实，只增不改）')
    w('')
    w('## 口径与假设')
    w('')
    w('- 每次合资格机会都计入试验（ITT），未领券机会净贡献计 0，不从分母剔除。')
    w('- 券的成交与撤单归发券时点的首个有效分配（门店-日），不随收银处当前组移动。')
    w('- 净贡献 = 实收净额 − 单位可变成本；撤单笔退回该笔实收，净贡献 = −单位成本（成本随履约沉没）。')
    w('- 核销后未满观察期、或券未核销且未过有效期：结果未定，记删失，不按零计入；'
      '正文给成熟口径，删失以区间上下界补充。')
    w('- 分配单位是门店-日，推断按门店-日聚类（CR1 校正），不假设顾客相互独立。')
    w('')

    w('## 归属与数据完整性')
    w('')
    w(f'- 合资格机会 {ig["eligible"]} 个；可归属 {ig["eligible"] - ig["excluded"]} 个，'
      f'无法确认归属 {ig["excluded"]} 个（{pct(ig["excluded_share"])}）。')
    for status, n in sorted(ig['excluded_by_status'].items()):
        label = {'conflict': '分配重叠且组别不一致', 'conflict_same_arm': '分配重叠但组别一致',
                 'missing_allocation': '缺少分配记录', 'broken_reference': '券/单引用断裂'}.get(status, status)
        w(f'  - {label}：{n} 个机会')
    for d in ig['conflict_days']:
        w(f'  - 重叠日：{d["shop"]} {d["on"]}')
    for d in ig['missing_days']:
        w(f'  - 缺分配：{d["shop"]} {d["on"]}')
    if ig['orphan_receipts'] or ig['orphan_vouchers']:
        w(f'  - 断引用的券 {len(ig["orphan_vouchers"])} 张、单 {len(ig["orphan_receipts"])} 笔')
    verdict = '仍支持预定的比较' if ig['supported'] else '不足以支持预定的比较，需先修复归属'
    days_txt = '、'.join(f'{k} {v} 个' for k, v in ig['days_per_arm'].items())
    w(f'- 结论：剔除无法确认部分后，各组有效门店-日：{days_txt}，{verdict}。')
    w('')

    w('## 主要比较：每次合资格机会的成熟净贡献')
    w('')
    w(_row(['组', '机会', '成熟', '删失', '发券', '核销', '撤单', '净贡献合计', '每次机会净贡献', '含删失下界', '含删失上界']))
    w(_row(['---'] * 11))
    for arm in result.arms:
        st = result.arm_stats[arm]
        w(_row([arm, st['opps'], st['complete'], st['censored'], st['issued'],
                st['redeemed'], st['refunds'], fen(st['net']), fen(st['per_opp']),
                fen(st['per_opp_lo']), fen(st['per_opp_hi'])]))
    w('')
    for arm, eff in result.effects.items():
        if eff is None:
            w(f'- {arm} − {result.baseline}：无法估计（成熟观测或聚类不足）。')
            continue
        ci = '无法估计' if eff['ci_lo'] is None else f'[{eff["ci_lo"]:.0f}分, {eff["ci_hi"]:.0f}分]'
        naive = '—' if eff['se_naive'] is None else f'{eff["se_naive"]:.0f}分'
        se_cl = '—' if eff['se_cluster'] is None else f'{eff["se_cluster"]:.0f}分'
        w(f'- {arm} − {result.baseline}：每次机会净增益 {eff["diff"]:.0f}分；'
          f'聚类稳健标准误 {se_cl}'
          f'（{eff["clusters"]} 个门店-日聚类），95% 区间 {ci}；'
          f'若错误假设顾客独立，标准误 {naive}。')
    w('')
    bounds_txt = []
    for a in result.effects:
        lo, hi = _effect_bounds(result, a)
        bounds_txt.append('无法给出界限' if lo is None else f'[{lo:.0f}分, {hi:.0f}分]')
    w('删失不计零：若把删失机会按最好/最坏情形计入，每次机会净增益介于 '
      + '、'.join(bounds_txt)
      + '。观察未成熟不是无效果的证据。')
    w('')
    w('### 日历日敏感性（描述性）')
    w('')
    w('各组在日历日上的切换完全同步，组与日期效应无法分离；下表仅供核对共同波动，'
      '正式推断以门店-日聚类为准。')
    w('')
    days = sorted({d for d, _ in result.day_cells})
    w(_row(['日期', '组', '成熟机会', '每次机会净贡献']))
    w(_row(['---'] * 4))
    for (day, arm), (mean, n) in result.day_cells.items():
        w(_row([day, arm, n, fen(mean)]))
    w('')

    w('## 偏离说明')
    w('')
    dev = result.deviations
    price_txt = '、'.join(f'{k} {v}分' for k, v in sorted(dev['arm_price'].items()))
    w(f'- 报价：各组基准报价 {price_txt}；'
      f'报价偏离 {len(dev["quote"])} 起。')
    for q in dev['quote'][:20]:
        w(f'  - {q["voucher"]}（{q["shop"]} {q["on"]}，{q["arm"]}）报价 {fen(q["quoted"])}，'
          f'应为 {fen(q["expected"])}')
    w(f'- 兑现：实收与报价不符 {len(dev["paid_mismatch"])} 起；'
      f'过期核销 {len(dev["expired_redemption"])} 起。')
    w(f'- 跨渠道：{len(dev["cross_channel"])} 笔在他店核销，'
      '归属仍记发券时分配，不随收银处移动。')
    for i in dev['interruptions']:
        if i['expects_stop']:
            mark = '符合停发' if i['compliant'] else f'停发日仍发券 {i["issued"]} 张（偏离）'
        else:
            mark = '非停发指令，发券事实如上'
        w(f'- 停发记录：{i["shop"]} {i["on"]}（{i["reason"]}）：当日发券 {i["issued"]} 张，{mark}。')
    for d in dev['issue_after_stop']:
        w(f'- 执行偏离：{d["shop"]}「{d["action"]}」{d["starts"]} 生效后仍发券 '
          f'{d["vouchers_issued"]} 张；这些券及其成交不因此从数据中抹除。')
    for d in dev['issue_on_conflict_day']:
        w(f'- 分配重叠日发券：{d["shop"]} {d["on"]} 发券 {d["issued"]} 张，'
          '归属无法确认，已排除出比较。')
    w('')

    w('## 门店决策')
    w('')
    w('停止发新券与继续履行已发出的券同时成立：处置只对新增发券生效，'
      '已发未兑券在有效期内仍按首次报价兑现，未取消的承诺不从数据中抹除。')
    w('')
    w(_row(['门店', '处置', '仍支持预定比较', '最新单位成本', '未兑现券', '待撤单笔', '复看日期']))
    w(_row(['---'] * 7))
    for shop, st in state['decisions'].items():
        cost = result.deviations['cost'].get(shop, {})
        w(_row([shop, st['decision'],
                '是' if st['supports_planned_comparison'] else '否',
                fen(cost.get('latest_cost')),
                len(st['honor_vouchers']), len(st['pending_refund_receipts']),
                st['revisit_on'] or '—']))
    w('')
    for shop, d in decisions.items():
        w(f'### {shop}：{d["decision"]}')
        for reason in d['reasons']:
            w(f'- {reason}')
        st = state['decisions'][shop]
        if st['honor_vouchers']:
            w(f'- 继续履行已发未兑券 {len(st["honor_vouchers"])} 张：'
              + '、'.join(st['honor_vouchers']))
        if st['pending_refund_receipts']:
            w(f'- 仍在撤单观察期 {len(st["pending_refund_receipts"])} 笔，'
              f'成熟后复跑更新判断（复看 {st["revisit_on"] or "—"}）。')
        for a in st['recorded_actions']:
            line = f'- 已登记行动：「{a["action"]}」{a["starts"]} 起（确认于 {a["confirmed_on"]}）'
            if 'execution_deviation' in a:
                line += f'；{a["execution_deviation"]}'
            w(line)
        w('')

    w('## 门店明细核对')
    w('')
    w('每个门店-日可核对到批次分配编号、组、发券报价与最终净额；'
      '净贡献 = 毛实收 − 撤单退回 − 单位成本（仅成熟口径，删失单列）。'
      '分配重叠日的金额在此列示用于对账，但不计入组间比较。')
    w('')
    shops = sorted({r.shop for r in result.records})
    for shop in shops:
        w(f'### {shop}')
        w('')
        w(_row(['日期', '分配批次', '组/状态', '机会', '发券', '核销', '成熟', '撤单',
                '毛实收', '退回', '成本', '净贡献', '删失']))
        w(_row(['---'] * 13))
        days = sorted({r.on for r in result.records if r.shop == shop})
        tot = defaultdict(int)
        for day in days:
            recs = [r for r in result.records if r.shop == shop and r.on == day and r.eligible]
            alloc_ids = sorted({aid for r in recs for aid in r.assignment.allocation_ids})
            arms = sorted({r.assignment.arm for r in recs if r.assignment.arm})
            st0 = recs[0].assignment.status
            if st0 == 'conflict':
                status = '分配重叠'
            elif st0 == 'missing_allocation':
                status = '缺分配'
            elif st0 == 'conflict_same_arm':
                status = f'{arms[0]}（重叠同组）'
            else:
                status = arms[0] if arms else '缺分配'
            fin = [r for r in recs if r.receipt is not None and r.value is not None]
            gross = sum(r.receipt['paid'] for r in fin)
            back = sum(r.receipt['paid'] for r in fin if r.state == ST_REFUNDED)
            cost = sum(r.receipt['cost'] for r in fin)
            net = sum(r.value for r in fin)
            row = [day, '/'.join(alloc_ids) or '—', status, len(recs),
                   sum(1 for r in recs if r.voucher), sum(1 for r in recs if r.receipt),
                   len(fin), sum(1 for r in fin if r.state == ST_REFUNDED),
                   fen(gross), fen(back), fen(cost), fen(net),
                   sum(1 for r in recs if r.state in CENSORED_STATES)]
            w(_row(row))
            for k, v in zip(('opps', 'issued', 'redeemed', 'mature', 'refunds',
                             'gross', 'back', 'cost', 'net', 'censored'),
                            (row[3], row[4], row[5], row[6], row[7],
                             gross, back, cost, net, row[12])):
                tot[k] += v
        w(_row(['合计', '', '', tot['opps'], tot['issued'], tot['redeemed'], tot['mature'],
                tot['refunds'], fen(tot['gross']), fen(tot['back']), fen(tot['cost']),
                fen(tot['net']), tot['censored']]))
        w('')
    total_net = sum(r.value for r in result.records if r.eligible and r.value is not None)
    attrib_net = sum(st['net'] for st in result.arm_stats.values())
    w(f'对账：各店成熟净贡献合计 {fen(total_net)} = 组间比较可归属 {fen(attrib_net)}'
      f' ＋ 归属无法确认部分 {fen(total_net - attrib_net)}。')
    w('')
    w('## 复跑与下期')
    w('')
    w('- 状态文件 `state.json` 给出各店处置、未了承诺与复看日期，可直接用于下期门店安排。')
    w('- 未到观察期的交易陆续成熟后，用同一命令对新快照复跑；判断会更新，'
      '但发券时点的报价安排不追溯改写。')
    w('- 下一轮换商品与分配表后，沿用同一资料结构（表名与字段不变）即可复用本工具。')
    w('')
    return '\n'.join(out)


def _effect_bounds(result, arm):
    """删失按最好/最坏情形计入时，每次机会净增益的区间。"""
    base = result.arm_stats[result.baseline]
    cur = result.arm_stats[arm]
    vals = (cur['per_opp_lo'], cur['per_opp_hi'], base['per_opp_lo'], base['per_opp_hi'])
    if any(v is None for v in vals):
        return None, None
    return cur['per_opp_lo'] - base['per_opp_hi'], cur['per_opp_hi'] - base['per_opp_lo']


LEDGER_FIELDS = [
    'opportunity', 'visitor', 'shop', 'on', 'eligible',
    'allocation_ids', 'assignment_status', 'arm',
    'voucher', 'quoted', 'issued_on', 'expires_on',
    'receipt', 'redeem_shop', 'redeemed_on', 'paid', 'refund_on', 'cost',
    'cross_channel', 'value_state', 'net_value', 'censored',
]


def write_ledger_csv(result, path):
    with open(path, 'w', encoding='utf-8', newline='') as fh:
        writer = csv.DictWriter(fh, fieldnames=LEDGER_FIELDS)
        writer.writeheader()
        for r in sorted(result.records, key=lambda x: (x.shop, x.on, x.opp_id)):
            v, rec = r.voucher, r.receipt
            writer.writerow({
                'opportunity': r.opp_id, 'visitor': r.visitor, 'shop': r.shop,
                'on': r.on, 'eligible': int(r.eligible),
                'allocation_ids': '/'.join(r.assignment.allocation_ids),
                'assignment_status': r.assignment.status,
                'arm': r.assignment.arm or '',
                'voucher': v['id'] if v else '',
                'quoted': v['quoted'] if v else '',
                'issued_on': v['issued_on'] if v else '',
                'expires_on': v['expires_on'] if v else '',
                'receipt': rec['id'] if rec else '',
                'redeem_shop': rec['shop'] if rec else '',
                'redeemed_on': rec['redeemed_on'] if rec else '',
                'paid': rec['paid'] if rec else '',
                'refund_on': rec['refund_on'] if rec and rec['refund_on'] else '',
                'cost': rec['cost'] if rec else '',
                'cross_channel': int(r.cross_channel),
                'value_state': r.state,
                'net_value': r.value if r.value is not None else '',
                'censored': int(r.state in CENSORED_STATES),
            })


def write_state_json(state, path):
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)
        fh.write('\n')
