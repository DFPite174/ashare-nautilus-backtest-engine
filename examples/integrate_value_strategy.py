"""Existing value_quant V targets -> frozen next-open account engine; validation only."""
import argparse
from collections import Counter
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime
from decimal import Decimal as D, ROUND_HALF_UP
import importlib.util
import json
from pathlib import Path
import sys
import time
import traceback
from zoneinfo import ZoneInfo
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from contracts.models import TargetWeights
from engine import prepare_backtest, run_prepared_backtest, plan_rebalance, DailyMarketRule, compare_complete_backtests
from engine.reproducibility import digest
from engine.tushare_lake import load_tushare_lake
from examples.value_data_snapshot import sha, build_snapshot, check_output_location
from examples.research_workflow import execution_outcomes
from examples.validate_tushare_lake import check_cash
from examples.benchmark_dense_lake import _peak_mib


def load_targets(root):
    root = Path(root)
    source = root/'src/value_quant/portfolio.py'
    provenance = dict(selector_sha256=sha(source), panels_sha256={}, holdout_evaluated=False,
                      variants=['V', 'R30'], top_n=30, control_seed='20261016')
    spec = importlib.util.spec_from_file_location('frozen_value_selector', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    folder = root/'outputs/history_provider_v1'
    provenance['schedule_sha256'] = sha(folder/'schedule.csv')
    schedule = pd.read_csv(folder/'schedule.csv', dtype=str)
    if sha(folder/'schedule.csv') != provenance['schedule_sha256']:
        raise ValueError('Upstream schedule changed while reading')
    steps = schedule[schedule.execution_date.between('20210101', '20231231')]
    if steps.empty or steps.signal_date.duplicated().any() or steps.execution_date.duplicated().any():
        raise ValueError('Missing or duplicate validation schedule')
    result = {variant: [] for variant in provenance['variants']}
    for step in steps.sort_values('signal_date').to_dict('records'):
        signal = datetime.strptime(step['signal_date'], '%Y%m%d').date()
        execution = datetime.strptime(step['execution_date'], '%Y%m%d').date()
        if signal >= execution:
            raise ValueError('Invalid signal/execution chronology')
        path = folder/f'panel_{step["signal_date"]}.csv'
        provenance['panels_sha256'][path.name] = sha(path)
        frame = pd.read_csv(path, dtype={'ts_code': str, 'signal_date': str})
        if not frame.signal_date.eq(step['signal_date']).all() or frame.ts_code.duplicated().any():
            raise ValueError('Invalid panel date or duplicate security')
        for variant in result:
            selected = module.build_targets(frame, variant, 30)
            weights = selected.loc[selected.target_weight.gt(0)].set_index('ts_code').target_weight.to_dict()
            if len(weights) > 30:
                raise ValueError('Upstream selection exceeds Top30')
            result[variant].append(TargetWeights(signal_date=signal, execution_date=execution,
                                                weights=weights, strategy_id=f'value-{variant}-next-open'))
        if sha(path) != provenance['panels_sha256'][path.name]:
            raise ValueError('Upstream panel changed while generating targets')
    if sha(source) != provenance['selector_sha256']:
        raise ValueError('Upstream selector changed while generating targets')
    config = root/'configs/history_research.json'
    if config.exists():
        provenance['upstream_config_sha256'] = sha(config)
    provenance['targets_sha256'] = digest({v: [t.model_dump(mode='json') for t in targets] for v, targets in result.items()})
    provenance['bundle_sha256'] = digest(provenance)
    return result, provenance


def replay(data, prepared, targets, provenance):
    schedule = {t.signal_date: t for t in targets}
    decisions, seen = [], set()
    days = data.trading_days
    def signal(closed, state):
        day = state.as_of.date()
        if day not in schedule:
            return []
        if day in seen:
            raise ValueError('Duplicate target decision callback')
        seen.add(day)
        target = schedule[day]
        index = days.index(day)
        if index+1 == len(days) or days[index+1] != target.execution_date:
            raise ValueError('Upstream execution date differs from official next session')
        symbols = set(target.weights) | set(state.positions)
        rules = {s: DailyMarketRule(**data.rules[s, day]) for s in symbols}
        plan = plan_rebalance(target, cash=state.available_cash, holdings=state.positions,
            sellable=state.sellable, prices={s: state.valuation_prices[s] for s in symbols},
            rules=rules, valuation_at=state.as_of, decision_at=state.as_of,
            cash_reserve=D(0), receivables=state.receivables)
        intents = [replace(i, intent_id=f'{day}:{i.symbol}:{i.side}') for i in plan.intents]
        assert all(state.valuation_times[s] <= state.as_of for s in symbols)
        decisions.append(dict(date=str(day), decision_time=state.as_of.isoformat(),
            execution_date=str(target.execution_date), weights=dict(target.weights),
            holdings=dict(state.positions), sellable=dict(state.sellable), cash=str(state.available_cash),
            nav=str(state.nav), allocations=[asdict(a) for a in plan.allocations],
            intents=[asdict(i) for i in intents]))
        return intents
    result = run_prepared_backtest(prepared, portfolio_signal=signal, initial_cash=D(1000000),
        participation_rate=.01, slippage_bps=10, data_mode='sparse',
        strategy_id=targets[0].strategy_id, strategy_params=dict(target_bundle=provenance['bundle_sha256'],
            policy='one submission; no chase; next month actual account feedback'),
        data_snapshot_id=data.snapshot['snapshot_id'])
    if seen != set(schedule):
        raise ValueError('Some frozen target decisions were not delivered')
    assert not result.pending_intents
    check_cash(result, 1000000)
    nav = {r['date']: r for r in result.daily_nav}
    for action in data.corporate_actions:
        expected = (D(nav[str(action.record_date)]['holdings'].get(action.symbol, 0))*action.cash_div).quantize(D('.01'), rounding=ROUND_HALF_UP)
        actual = sum((D(r['amount']) for r in result.account_journal if r['event_id'].startswith(
            f'{action.symbol}|{action.ex_date}|') and r['event_id'].endswith('|dividend')), D(0))
        assert actual == expected, (action.symbol, action.ex_date, actual, expected)
    check_share_actions(data, result)
    return result, decisions


def fractional_cash_expected(quantity, ratio, previous_close):
    expanded = quantity*ratio
    return ((expanded-D(int(expanded)))*previous_close/ratio).quantize(D('.01'), rounding=ROUND_HALF_UP)


def check_share_actions(data, result):
    """Independent quantity conservation and approved cash formula, not real CSDC allocation."""
    nav = {r['date']: r for r in result.daily_nav}
    closes = {(b.symbol, b.end.date()): D(str(b.close)) for b in data.bars}
    for action in data.corporate_actions:
        ratio = D(1)+action.bonus_ratio+action.reserve_ratio
        if ratio == 1:
            continue
        previous = [day for day in data.trading_days if day < action.ex_date]
        quantity = D(nav[str(previous[-1])]['holdings'].get(action.symbol, 0)) if previous else D(0)
        expanded = quantity*ratio
        net = sum((D(f['quantity'])*(1 if f['side']=='BUY' else -1) for f in result.fills
            if f['symbol']==action.symbol and datetime.fromtimestamp(f['time']/1e9, ZoneInfo('Asia/Shanghai')).date()==action.ex_date), D(0))
        assert D(nav[str(action.ex_date)]['holdings'].get(action.symbol, 0)) == D(int(expanded))+net
        expected_cash = D(0)
        if expanded != int(expanded):
            if action.fractional_policy != 'cash_in_lieu' or (action.symbol, previous[-1]) not in closes:
                raise ValueError('Fraction verification requires approved cash model and fresh previous-session close')
            expected_cash = fractional_cash_expected(quantity, ratio, closes[action.symbol, previous[-1]])
        actual_cash = sum((D(r['amount']) for r in result.account_journal if r['event_id'].startswith(
            f'{action.symbol}|{action.ex_date}|') and r['event_id'].endswith('|fraction')), D(0))
        assert actual_cash == expected_cash


def metrics(result):
    rows = [r for r in result.daily_nav if '2021-01-01' <= r['date'] <= '2023-12-31']
    values = pd.Series([1000000.] + [float(r['total_equity']) for r in rows])
    returns = values.pct_change().dropna()
    return dict(start=rows[0]['date'], end=rows[-1]['date'], sessions=len(rows),
        total_return=values.iloc[-1]/1000000-1,
        annualized_return=(values.iloc[-1]/1000000)**(252/len(rows))-1,
        max_drawdown=float((values/values.cummax()-1).min()),
        sharpe_zero_rf=float(returns.mean()/returns.std(ddof=1)*252**.5) if returns.std(ddof=1) else None)


def action_failure_context(error):
    for frame, _ in traceback.walk_tb(error.__traceback__):
        values = frame.f_locals
        if frame.f_code.co_name == '_start_day' and all(k in values for k in ('a', 'pos', 'ratio')):
            action, position = values['a'], values['pos']
            quantity = D(str(position.quantity))
            return dict(symbol=action.symbol, ex_date=str(action.ex_date),
                native_quantity=str(quantity), share_ratio=str(D(1)+action.bonus_ratio+action.reserve_ratio),
                mathematical_quantity=str(quantity*(D(1)+action.bonus_ratio+action.reserve_ratio)),
                fractional_policy=action.fractional_policy)
    return None


def public_summary(report):
    public = deepcopy(report)
    snapshot = public.get('derived_snapshot', {})
    if 'symbols' in snapshot:
        snapshot['symbols_sha256'] = digest(snapshot.pop('symbols'))
    for run in public['runs']:
        config = run['manifest']['config']
        config['symbol_fee_policy_sha256'] = digest(config.pop('symbol_fee_policy'))
    public['redacted_fields'] = ['derived_snapshot.symbols', 'runs.manifest.config.symbol_fee_policy']
    return public


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--strategy-root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--fractional-policy', choices=['reject', 'cash_in_lieu'], default='reject')
    args = parser.parse_args()
    args.output = check_output_location(args.output, args.root, args.strategy_root)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output/'summary.json').unlink(missing_ok=True)
    report = dict(status='RUNNING', holdout_evaluated=False, historical_vintage_pit=False,
        execution_variant='raw price / next-session open / lots and actual account / 10 bps slippage',
        fractional_policy=args.fractional_policy, deferred_personal_dividend_tax_modeled=False,
        fractional_cash_model='previous raw close / split ratio; research approximation, not actual fractional allocation',
        scope='Tushare provider-basis validation; not original NAV replication or independent OOS', runs=[])
    started = time.perf_counter()
    try:
        targets, provenance = load_targets(args.strategy_root)
        expected_months = [f'{year}{month:02}' for year in range(2021, 2024) for month in range(1, 13)]
        if any([t.execution_date.strftime('%Y%m') for t in group] != expected_months for group in targets.values()):
            raise ValueError('Require all 36 chronological monthly validation targets')
        report['upstream'] = provenance
        symbols = sorted({s for group in targets.values() for target in group for s in target.weights})
        report['counts'] = dict(symbols=len(symbols), targets={v: len(t) for v, t in targets.items()})
        target_file = args.output/'frozen_targets.json'
        target_file.write_text(json.dumps({v: [t.model_dump(mode='json') for t in group] for v, group in targets.items()}, indent=2), encoding='utf-8')
        report['target_artifact_sha256'] = sha(target_file)
        view = args.output/'derived_lake'
        report['derived_snapshot'] = build_snapshot(args.root, view, symbols, '2020-12-01', '2023-12-29',
            Path(__file__).parent/'data/value_action_resolutions.json')
        data = load_tushare_lake(view, symbols, '2020-12-01', '2023-12-29')
        if args.fractional_policy == 'cash_in_lieu':
            data.corporate_actions = [a.model_copy(update={'fractional_policy': 'cash_in_lieu'})
                if a.bonus_ratio+a.reserve_ratio > 0 else a for a in data.corporate_actions]
        report['execution_actions_sha256'] = digest([a.model_dump(mode='json') for a in data.corporate_actions])
        report['counts'].update(bars=len(data.bars), sessions=len(data.trading_days), actions=len(data.corporate_actions))
        report['market_snapshot'] = data.snapshot
        prepared = prepare_backtest(data.instruments, data.bars, data.rules,
            trading_days=data.trading_days, corporate_actions=data.corporate_actions)
        reference = None
        for name, variant in [('V', 'V'), ('R30', 'R30'), ('V-repeat', 'V')]:
            tick = time.perf_counter()
            result, decisions = replay(data, prepared, targets[variant], provenance)
            outcomes = execution_outcomes(decisions, result)
            if name == 'V':
                reference = result
            if name == 'V-repeat':
                report['repeat_comparison'] = compare_complete_backtests(reference, result)
                assert report['repeat_comparison']['equal']
            artifact = args.output/f'{name}.json'
            artifact.write_text(json.dumps(dict(result=result.to_dict(), decisions=decisions, execution_outcomes=outcomes),
                default=str, ensure_ascii=False, indent=2), encoding='utf-8')
            row = dict(name=name, fills=len(result.fills), rejections=len(result.rejections),
                decisions=len(decisions),
                max_positions=max(sum(D(q)>0 for q in r['holdings'].values()) for r in result.daily_nav),
                fractional_cash_events=sum(r['event_id'].endswith('|fraction') for r in result.account_journal),
                fractional_cash_total=str(sum((D(r['amount']) for r in result.account_journal if r['event_id'].endswith('|fraction')), D(0))),
                rejection_reasons=dict(Counter(r['reason'] for r in result.rejections)),
                planning_reasons=dict(Counter(reason for d in decisions for a in d['allocations'] for reason in a['reasons'])),
                metrics=metrics(result), seconds=time.perf_counter()-tick, manifest=result.manifest,
                artifact_sha256=sha(artifact))
            report['runs'].append(row)
            print({k: v for k, v in row.items() if k != 'manifest'}, flush=True)
        report['status'] = 'PASS_EXECUTION_ADAPTATION_NOT_ALPHA_CERTIFICATION'
    except Exception as error:
        report.update(status='BLOCKED_OR_FAILED', error_type=type(error).__name__, reason=str(error),
                      action_context=action_failure_context(error))
        raise
    finally:
        report.update(seconds=time.perf_counter()-started, peak_working_set_mib=_peak_mib(),
            script_sha256=sha(__file__), data_preparation_sha256=sha(Path(__file__).with_name('value_data_snapshot.py')))
        private = args.output/'summary.json'
        private.write_text(json.dumps(report, default=str, ensure_ascii=False, indent=2), encoding='utf-8')
        public = public_summary(report)
        public['private_summary_sha256'] = sha(private)
        (args.output/'public_summary.json').write_text(json.dumps(public, default=str, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
