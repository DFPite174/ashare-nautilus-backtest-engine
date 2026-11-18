"""Replay the same frozen candidates with VectorBT and the A-share event engine."""
import argparse
from collections import Counter
from datetime import datetime
from decimal import Decimal as D, ROUND_HALF_UP
import json
import os
from pathlib import Path
import subprocess
import sys
from time import perf_counter
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def candidate_orders(opens, targets):
    sizes = opens.copy() * np.nan
    seen = set()
    for row in targets:
        signal, day = row['signal_date'], row['execution_date']
        index = opens.index.get_loc(day)
        if day in seen or index == 0 or opens.index[index - 1] != signal:
            raise ValueError('Invalid frozen target chronology')
        seen.add(day)
        weights = row['weights']
        if not set(weights) <= set(opens.columns):
            raise ValueError('Target security outside frozen market')
        sizes.loc[day] = pd.Series(weights, dtype=float).reindex(opens.columns, fill_value=0.)
    return sizes.where(opens.notna())


def target_drift(target, actual):
    return sum(abs(target.get(s, 0) - actual.get(s, 0)) for s in set(target) | set(actual))


def run_vector(payload):
    import vectorbt as vbt
    started = perf_counter()
    close = pd.DataFrame(payload['close'], index=payload['days'], dtype=float)
    opens = pd.DataFrame(payload['open'], index=close.index, columns=close.columns, dtype=float)
    results = {}
    for name, targets in payload['cases'].items():
        if name == 'common':
            sizes = close * np.nan
            fixed = close * 0.
            for order in targets:
                day, symbol = order['date'], order['symbol']
                sizes.loc[day, symbol] = order['quantity']
                fixed.loc[day, symbol] = order['fee']
            options = dict(size_type='amount', fixed_fees=fixed, allow_partial=False)
        else:
            sizes = candidate_orders(opens, targets)
            options = dict(size_type='targetpercent', val_price=close.ffill().shift(1).fillna(-np.inf),
                           fixed_fees=5., fees=.00031, slippage=.001, allow_partial=True)
        portfolio = vbt.Portfolio.from_orders(
            close, size=sizes, price=opens, init_cash=payload['initial_cash'], cash_sharing=True,
            group_by=True, call_seq='auto', size_granularity=100, **options)
        positions = portfolio.assets()
        daily = [
            dict(date=day, cash=float(cash), equity=float(equity),
                 positions={s: int(q) for s, q in row.items() if q})
            for (day, row), cash, equity in zip(positions.iterrows(), portfolio.cash(), portfolio.value())
        ]
        fills = sorted([
            dict(date=close.index[r.idx], symbol=close.columns[r.col], side='BUY' if r.side == 0 else 'SELL',
                 quantity=int(r.size), price=float(r.price), fee=float(r.fees))
            for r in portfolio.orders.records.itertuples()
        ], key=lambda r: (r['date'], r['symbol'], r['side']))
        results[name] = dict(daily=daily, fills=fills)
    return dict(version=vbt.__version__, seconds=perf_counter()-started, results=results)


def account(result):
    tz = ZoneInfo('Asia/Shanghai')
    fills = [
        dict(date=str(datetime.fromtimestamp(f['time']/1e9, tz).date()), symbol=f['symbol'], side=f['side'],
             quantity=int(D(f['quantity'])), price=f['price'], fee=f['fee'])
        for f in result['fills']
    ]
    return dict(fills=sorted(fills, key=lambda f: (f['date'], f['symbol'], f['side'])),
                daily=[dict(date=r['date'], cash=r['cash'], equity=r['total_equity'],
                            positions={s: int(D(q)) for s, q in r['holdings'].items() if D(q)})
                       for r in result['daily_nav']])


def matrix(data, initial_cash, cases):
    days = [str(day) for day in data.trading_days]
    values = {field: pd.DataFrame(index=days, columns=sorted(i.symbol for i in data.instruments), dtype=float)
              for field in ('open', 'close')}
    for bar in data.bars:
        for field, frame in values.items():
            frame.at[str(bar.end.date()), bar.symbol] = float(getattr(bar, field))
    return dict(days=days, initial_cash=initial_cash, cases=cases,
                **{k: v.astype(object).where(v.notna(), None).to_dict('list') for k, v in values.items()})


def describe(result, targets, payload):
    rows = [r for r in result['daily'] if r['date'] >= targets[0]['execution_date']]
    equity = pd.Series([float(payload['initial_cash'])] + [float(r['equity']) for r in rows])
    by_day = {r['date']: r for r in rows}
    quotes = pd.DataFrame(payload['close'], index=payload['days'], dtype=float).ffill()
    misses, residuals, drifts = [], [], []
    for target in targets:
        day = target['execution_date']
        row = by_day[day]
        held = row['positions']
        misses.append(len(target['weights'].keys() - held.keys()))
        residuals.append(len(held.keys() - target['weights'].keys()))
        # Raw market closes are a common mark; exact event valuation remains in its account.
        actual = {s: q * quotes.at[day, s] / float(row['equity']) for s, q in held.items()}
        drifts.append(target_drift(target['weights'], actual))
    return dict(total_return=float(equity.iloc[-1]/equity.iloc[0]-1),
                max_drawdown=float((equity/equity.cummax()-1).min()),
                fills=len(result['fills']), fees=float(sum((D(str(f['fee'])) for f in result['fills']), D(0))),
                execution_days=len(targets), missing_target_stock_days=sum(misses),
                residual_stock_days=sum(residuals), mean_absolute_stock_weight_gap=float(np.mean(drifts)))


def main():
    from contracts.models import TargetWeights
    from engine import OrderIntent, prepare_backtest, run_backtest
    from engine.tushare_lake import load_tushare_lake
    from examples.compare_common_broker import compare_accounts
    from examples.integrate_value_strategy import replay
    from examples.run_value_p1 import economic_path
    from examples.validate_tushare_lake import check_cash
    from examples.value_data_snapshot import check_output_location, sha

    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ('root', 'baseline', 'vector-python', 'output'):
        parser.add_argument('--'+flag, type=Path, required=True)
    args = parser.parse_args()
    output = check_output_location(args.output, args.root, args.baseline)
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError('Use an empty output directory')

    def vector(name, inputs):
        path = output/f'{name}-input.json'
        artifact = output/f'{name}-vector.json'
        path.write_text(json.dumps(inputs, allow_nan=False), encoding='utf-8')
        env = dict(os.environ, NUMBA_CACHE_DIR=str(output/'numba'))
        start = perf_counter()
        subprocess.run([str(args.vector_python.resolve()), '-B', str(Path(__file__).resolve()),
                        '--worker', str(path), str(artifact)], env=env, check=True)
        result = json.loads(artifact.read_text(encoding='utf-8'))
        result['process_seconds'] = perf_counter()-start
        return result

    start = perf_counter()
    sample = load_tushare_lake(args.root, ['000001.SZ', '600000.SH'], '2023-12-21', '2023-12-29')
    if sample.corporate_actions or len(sample.bars) != 14 or len(sample.trading_days) != 7:
        raise ValueError('Common sample must cover seven sessions and no corporate actions')
    for b in sample.bars:
        rule = sample.rules[b.symbol, b.start.date()]
        if (rule['trade_status'] != 'OPEN' or rule['limit_state'] != 'BOUNDED' or b.volume < 10000
                or not D(str(rule['down_limit'])) < b.open < D(str(rule['up_limit']))):
            raise ValueError('Common sample fails trade admission')
    orders = []
    for b in sample.bars:
        day = b.start.date()
        if day in sample.trading_days[1:3]:
            sell = day == sample.trading_days[2]
            amount = b.open * 100
            fee = (max(D(5), amount*D('.0003')) + amount*(D('.00001') + (D('.0005') if sell else 0)))
            orders.append(dict(date=str(day), symbol=b.symbol, quantity=-100 if sell else 100,
                               fee=float(fee.quantize(D('.01'), rounding=ROUND_HALF_UP))))
    def signal(bar, state):
        if bar.end.date() in sample.trading_days[:2]:
            return [OrderIntent(bar.symbol, 'BUY' if bar.end.date() == sample.trading_days[0] else 'SELL', 100)]
        return []
    own = run_backtest(sample.instruments, sample.bars, sample.rules, signal, initial_cash=D(100000),
                       trading_days=sample.trading_days, participation_rate=.01)
    check_cash(own, 100000)
    common_vector = vector('common', matrix(sample, 100000, {'common': orders}))
    common = compare_accounts(account(own.to_dict()), common_vector['results']['common'])
    if not common['equal'] or len(own.fills) != 4 or own.rejections or own.pending_intents:
        raise ValueError(f'Common account mismatch: {common}')
    (output/'common-event.json').write_text(json.dumps(account(own.to_dict())), encoding='utf-8')
    print('Common case: four fills and seven daily accounts match', flush=True)

    base = args.baseline
    source = json.loads((base/'summary.json').read_text(encoding='utf-8'))
    if source['holdout_evaluated'] or source['fractional_policy'] != 'cash_in_lieu':
        raise ValueError('Expected sealed validation run with the recorded cash policy')
    lake = base/'derived_lake'
    checked = {lake/p: h for p, h in source['derived_snapshot']['derived_files_sha256'].items()}
    checked[base/'frozen_targets.json'] = source['target_artifact_sha256']
    for name in ('V', 'R30'):
        checked[base/f'{name}.json'] = next(row['artifact_sha256'] for row in source['runs'] if row['name'] == name)
    def check_inputs():
        if any(sha(p) != expected for p, expected in checked.items()):
            raise ValueError('Frozen input mismatch')
    check_inputs()
    targets = json.loads((base/'frozen_targets.json').read_text(encoding='utf-8'))
    if set(targets) != {'V', 'R30'} or any(len(group) != 36 for group in targets.values()):
        raise ValueError('Expected the frozen 36-month V and R30 schedules')
    data = load_tushare_lake(lake, source['derived_snapshot']['symbols'], '2020-12-01', '2023-12-29')
    data.corporate_actions = [a.model_copy(update={'fractional_policy':'cash_in_lieu'})
                              if a.bonus_ratio+a.reserve_ratio > 0 else a for a in data.corporate_actions]
    preparation = perf_counter()
    prepared = prepare_backtest(data.instruments, data.bars, data.rules, trading_days=data.trading_days,
                                corporate_actions=data.corporate_actions)
    prepare_seconds = perf_counter()-preparation
    inputs = matrix(data, 1000000, targets)
    screening = vector('candidates', inputs)
    report = dict(common=dict(**common, fills=4, sessions=7), vectorbt=screening['version'],
                  source_target_sha256=source['target_artifact_sha256'], source_snapshot_id=data.snapshot['snapshot_id'],
                  source_candidate_sha256={name:checked[base/f'{name}.json'] for name in targets},
                  script_sha256=sha(__file__), symbols=len(data.instruments), holdout_evaluated=False,
                  vector_process_seconds=screening['process_seconds'], vector_work_seconds=screening['seconds'],
                  event_prepare_seconds=prepare_seconds, runs={})
    for name, group in targets.items():
        tick = perf_counter()
        result, decisions = replay(data, prepared, [TargetWeights.model_validate(row) for row in group], source['upstream'])
        actual = result.to_dict()
        old = json.loads((base/f'{name}.json').read_text(encoding='utf-8'))
        if economic_path(actual) != economic_path(old['result']) or [d['weights'] for d in decisions] != [r['weights'] for r in group]:
            raise ValueError(f'Event side differs from frozen {name}')
        event_seconds = perf_counter()-tick
        full = account(actual)
        fast = screening['results'][name]
        report['runs'][name] = dict(fast=describe(fast, group, inputs), event=describe(full, group, inputs),
            event_seconds=event_seconds, event_matches_frozen=True,
            event_rejections=dict(Counter(r['reason'] for r in result.rejections)),
            event_planning=dict(Counter(reason for d in decisions for a in d['allocations'] for reason in a['reasons'])))
        (output/f'{name}-event.json').write_text(json.dumps(actual, default=str), encoding='utf-8')
        print(name, report['runs'][name], flush=True)
    check_inputs()
    report['return_rank'] = {mode:sorted(targets, key=lambda n: -report['runs'][n][mode]['total_return'])
                             for mode in ('fast', 'event')}
    report['elapsed_seconds'] = perf_counter()-start
    (output/'summary.json').write_text(json.dumps(report, indent=2, allow_nan=False), encoding='utf-8')


if __name__ == '__main__':
    if len(sys.argv) == 4 and sys.argv[1] == '--worker':
        result = run_vector(json.loads(Path(sys.argv[2]).read_text(encoding='utf-8')))
        Path(sys.argv[3]).write_text(json.dumps(result, allow_nan=False), encoding='utf-8')
    else:
        main()
