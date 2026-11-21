"""命令行入口：python3 -m pricing_experiment 数据.json [--out 输出目录] [--baseline 组名]

复跑同一命令即可重算；输出确定性内容，便于核对与留档。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .core import DataError, analyze, load_data
from .decide import build_state, decide
from .report import render_markdown, write_ledger_csv, write_state_json


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='pricing_experiment',
        description='分店定价试验：按实际分配比较每次合资格机会的成熟净贡献')
    parser.add_argument('data', help='资料 JSON（表结构见 README）')
    parser.add_argument('--out', default='out', help='输出目录（默认 out/）')
    parser.add_argument('--baseline', default=None, help='基准组名（默认取首个分配日的组）')
    args = parser.parse_args(argv)

    try:
        data, source_hash = load_data(args.data)
        result = analyze(data, source_hash=source_hash, baseline=args.baseline)
    except DataError as exc:
        print(f'资料校验未通过：{exc}', file=sys.stderr)
        return 2

    decisions = decide(result)
    state = build_state(result, decisions, data['actions'])

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    report = render_markdown(result, decisions, state)
    (outdir / 'report.md').write_text(report, encoding='utf-8')
    write_ledger_csv(result, outdir / 'attribution_ledger.csv')
    write_state_json(state, outdir / 'state.json')

    print(f'输出目录：{outdir}/（report.md、attribution_ledger.csv、state.json）')
    print(f'归属完整性：{result.integrity["eligible"] - result.integrity["excluded"]}'
          f'/{result.integrity["eligible"]} 可归属；'
          f'{"仍支持预定的比较" if result.integrity["supported"] else "不足以支持预定的比较"}')
    for arm, eff in result.effects.items():
        if eff and eff['ci_lo'] is not None:
            print(f'效果 {arm}−{result.baseline}：每次机会 {eff["diff"]:.0f}分，'
                  f'95%区间 [{eff["ci_lo"]:.0f}, {eff["ci_hi"]:.0f}]'
                  f'（{eff["clusters"]} 个门店-日聚类）')
    for shop, d in decisions.items():
        print(f'门店 {shop}：{d["decision"]}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
