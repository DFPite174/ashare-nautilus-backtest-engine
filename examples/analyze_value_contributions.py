"""Read-only security profit contributions and frozen target exposures; no causal attribution."""
import argparse
from collections import defaultdict
from decimal import Decimal as D
import json
import math
from pathlib import Path
import sys

import pandas as pd
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import compare_backtests
from examples.value_data_snapshot import sha, check_output_location


def security_pnl(result, closing_prices, initial_cash):
    pnl = defaultdict(D)
    for fill in result['fills']:
        sign = 1 if fill['side']=='SELL' else -1
        pnl[fill['symbol']] += sign*D(fill['quantity'])*D(fill['price'])-D(fill['fee'])
    for row in result['account_journal']:
        pnl[row['event_id'].split('|')[0]] += D(row['amount'])
    last = result['daily_nav'][-1]
    values = {s:D(q)*closing_prices[s] for s,q in last['holdings'].items() if D(q)}
    if sum(values.values(), D(0)) != D(last['positions_value']):
        raise ValueError('Final valuation does not reconcile')
    if D(last['receivable_dividends']):
        raise ValueError('Security allocation of ending receivables is not supported')
    for symbol, value in values.items():
        pnl[symbol] += value
    if sum(pnl.values(), D(0)) != D(last['total_equity'])-initial_cash:
        raise ValueError('Security profits do not reconcile')
    return dict(pnl)


def target_exposure(weights, panel):
    sectors = defaultdict(float)
    weighted_yield = 0.
    for symbol, weight in weights.items():
        if symbol not in panel:
            raise ValueError(f'Missing target panel row: {symbol}')
        row = panel[symbol]
        value = float(row['value_yield'])
        if not math.isfinite(value):
            raise ValueError('Non-finite target earnings yield')
        weight = float(weight)
        sectors[row['industry'] or 'UNKNOWN'] += weight
        weighted_yield += weight*value
    return dict(industry_weights=dict(sectors), weighted_value_yield=weighted_yield)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--strategy-root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output = check_output_location(args.output, args.run, args.strategy_root)
    source_file = args.run/'summary.json'
    source_hash = sha(source_file)
    source = json.loads(source_file.read_text(encoding='utf-8'))
    if source['holdout_evaluated'] or not source['status'].startswith('PASS_EXECUTION_ADAPTATION'):
        raise ValueError('Require frozen successful validation-only integration')
    protected = {source_file:source_hash}
    artifacts = {}
    for name in ('V','R30'):
        path = args.run/f'{name}.json'
        protected[path] = next(r['artifact_sha256'] for r in source['runs'] if r['name']==name)
        if sha(path)!=protected[path]:
            raise ValueError('Frozen result changed')
        artifacts[name] = json.loads(path.read_text(encoding='utf-8'))
        dates = [r['date'] for r in artifacts[name]['result']['daily_nav']]
        if not dates or any(not '2020-12-01'<=day<='2023-12-31' for day in dates):
            raise ValueError('Unexpected NAV dates outside validation/warmup')
    if not compare_backtests(artifacts['V']['result'],artifacts['R30']['result'])['comparable_model']:
        raise ValueError('Economic models differ')
    prices = {}
    end = max(r['date'] for r in artifacts['V']['result']['daily_nav']).replace('-','')
    for relative, expected in sorted(source['derived_snapshot']['derived_files_sha256'].items()):
        if '/market/daily/' not in relative:
            continue
        path = args.run/'derived_lake'/relative
        protected[path] = expected
        if sha(path)!=expected:
            raise ValueError('Frozen market input changed')
        frame = pq.read_table(path,columns=['ts_code','trade_date','close']).to_pandas()
        frame = frame[frame.trade_date<=end].sort_values('trade_date').groupby('ts_code').tail(1)
        for row in frame.itertuples():
            if row.ts_code not in prices or row.trade_date>prices[row.ts_code][0]:
                prices[row.ts_code] = row.trade_date,D(str(row.close))
    panels, industries = {}, defaultdict(set)
    for filename, expected in source['upstream']['panels_sha256'].items():
        if not '20201231'<=filename.removeprefix('panel_').removesuffix('.csv')<='20231231':
            raise ValueError('Unexpected signal panel date')
        path = args.strategy_root/'outputs/history_provider_v1'/filename
        protected[path] = expected
        if sha(path)!=expected:
            raise ValueError('Frozen signal panel changed')
        frame = pd.read_csv(path,dtype={'ts_code':str,'signal_date':str},keep_default_na=False)
        if frame.ts_code.duplicated().any():
            raise ValueError('Duplicate panel securities')
        panel = frame.set_index('ts_code').to_dict('index')
        panels[filename[6:-4]] = panel
        for symbol,row in panel.items():
            industries[symbol].add(row['industry'] or 'UNKNOWN')
    report = dict(source_summary_sha256=source_hash, script_sha256=sha(__file__),
        holdout_evaluated=False, analysis='Observed profit contributions and target exposures; not causal attribution',
        industry_basis='Upstream panel proxy; not historical industry membership',cases={})
    private = {}
    for name,artifact in artifacts.items():
        result = artifact['result']
        pnl = security_pnl(result,{s:p for s,(_,p) in prices.items()},D(result['manifest']['config']['initial_cash']))
        # Stable proxy labels only; changing or missing labels stay in explicit buckets.
        sector_pnl = defaultdict(D)
        labels = {s:next(iter(industries[s])) if len(industries[s])==1 else
                  'MULTIPLE_LABELS' if industries[s] else 'UNKNOWN' for s in pnl}
        for symbol,value in pnl.items():
            sector_pnl[labels[symbol]] += value
        exposures = []
        mean_sectors = defaultdict(float)
        for decision in artifact['decisions']:
            panel = panels[decision['date'].replace('-','')]
            exposure = target_exposure(decision['weights'],panel)
            exposures.append(dict(date=decision['date'],**exposure))
            for sector,weight in exposure['industry_weights'].items():
                mean_sectors[sector] += weight/len(artifact['decisions'])
        annual,previous = {},D(result['manifest']['config']['initial_cash'])
        for year in ('2021','2022','2023'):
            rows = [r for r in result['daily_nav'] if r['date'].startswith(year)]
            final = D(rows[-1]['total_equity'])
            annual[year] = dict(pnl=str(final-previous),return_percent=float((final/previous-1)*100))
            previous = final
        ranked = sorted(pnl.values(),reverse=True)
        report['cases'][name] = dict(net_pnl=str(sum(pnl.values(),D(0))), annual=annual,
            positive_contributors=sum(v>0 for v in pnl.values()),negative_contributors=sum(v<0 for v in pnl.values()),
            top_five_contribution=str(sum(ranked[:5],D(0))),bottom_five_contribution=str(sum(ranked[-5:],D(0))),
            industry_profit_cny={s:str(v) for s,v in sorted(sector_pnl.items()) if v},
            mean_target_industry_weights=dict(sorted(mean_sectors.items())),
            mean_target_value_yield=sum(e['weighted_value_yield'] for e in exposures)/len(exposures),
            decisions=len(exposures))
        private[name] = dict(security_profit_cny={s:str(v) for s,v in pnl.items() if v},
            industry_labels=labels,monthly_target_exposures=exposures,
            terminal_price_dates={s:prices[s][0] for s,q in result['daily_nav'][-1]['holdings'].items() if D(q)})
    for path, expected in protected.items():
        if sha(path)!=expected:
            raise ValueError('Input changed during analysis')
    report['input_sha256'] = {str(p.relative_to(args.run)) if p.is_relative_to(args.run) else p.name:h
                              for p,h in protected.items()}
    args.output.mkdir(parents=True,exist_ok=True)
    detail = args.output/'private_details.json'
    detail.write_text(json.dumps(private,ensure_ascii=False,indent=2),encoding='utf-8')
    report['private_details_sha256'] = sha(detail)
    report['status'] = 'RECONCILED_DESCRIPTIVE_ANALYSIS'
    (args.output/'summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__ == '__main__':
    main()
