"""Fixed weekly candidate/account-feedback acceptance, not an alpha claim."""
import argparse
from collections import Counter
from datetime import datetime
from dataclasses import asdict, replace
from decimal import Decimal as D, ROUND_HALF_UP
import hashlib
import json
from pathlib import Path
import sys
import time
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from contracts.models import TargetWeights
from engine import plan_rebalance, prepare_backtest, run_prepared_backtest, compare_complete_backtests
from engine.market_rules import DailyMarketRule
from engine.tushare_lake import load_tushare_lake
from examples.validate_tushare_lake import check_cash

SYMBOLS = ['000001.SZ', '002128.SZ', '600000.SH', '600519.SH']


def target_weights(history, filtered):
    eligible = sorted(s for s, prices in history.items()
                      if not filtered or (len(prices) >= 20 and
                          prices[-1] > sum(prices[-20:], D(0)) / 20))
    return {s: 1 / len(eligible) for s in eligible}


def execution_outcomes(decisions, result):
    """Join this workflow's planned intents to observed execution, not inferred causes."""
    data = result.to_dict() if hasattr(result, 'to_dict') else result
    rows = {}
    for decision in decisions:
        for intent in decision['intents']:
            identity = intent.get('intent_id')
            if not identity or identity in rows:
                raise ValueError('Unique nonempty planned intent_id required')
            rows[identity] = dict(intent_id=identity, symbol=intent['symbol'], side=intent['side'],
                decision_time=decision['decision_time'], execution_date=decision['execution_date'],
                requested_quantity=intent['quantity'], filled_quantity=D(0), fees=D(0),
                rejection_reasons=[], pending_reasons=[], native_statuses=[])
    for name in ('fills', 'rejections', 'orders', 'pending_intents'):
        for event in data[name]:
            identity = event.get('intent_id')
            if identity not in rows:
                raise ValueError(f'Unmatched {name} intent_id: {identity}')
            row = rows[identity]
            if name == 'fills':
                if event['symbol'] != row['symbol'] or event['side'] != row['side']:
                    raise ValueError(f'Fill identity mismatch: {identity}')
                row['filled_quantity'] += D(str(event['quantity']))
                row['fees'] += D(str(event['fee']))
            elif name == 'rejections':
                row['rejection_reasons'].append(event['reason'])
            elif name == 'orders':
                row['native_statuses'].append(event['status'])
            else:
                row['pending_reasons'].append(event['reason'])
    for row in rows.values():
        filled, requested = row['filled_quantity'], D(row['requested_quantity'])
        if not filled.is_finite() or not 0 <= filled <= requested:
            raise ValueError(f'Invalid filled quantity: {row["intent_id"]}')
        row['unfilled_quantity'] = str(requested - filled)
        row['outcome'] = ('FILLED' if filled == requested else 'PARTIALLY_FILLED' if filled else
                          'REJECTED' if row['rejection_reasons'] else
                          'PENDING' if row['pending_reasons'] else 'UNFILLED')
        row['filled_quantity'], row['fees'] = str(filled), str(row['fees'])
    return list(rows.values())


def run_candidate(data, filtered, initial, participation, slippage, prepared=None):
    days = data.trading_days
    indexes = {day: i for i, day in enumerate(days)}
    decisions = []

    def signal(closed, state):
        day = state.as_of.date()
        index = indexes[day]
        if index % 5 or index + 1 == len(days):
            return []
        weights = target_weights({s: [D(str(b.close)) for b in state.history.get(s, ())]
                                  for s in SYMBOLS if s in {i.symbol for i in data.instruments}}, filtered)
        target = TargetWeights(signal_date=day, execution_date=days[index + 1], weights=weights)
        symbols = set(weights) | set(state.positions)
        # Only today's known rules are available at the decision, never tomorrow's status.
        rules = {s: data.rules[(s, day)] for s in symbols}
        rules = {s: r if isinstance(r, DailyMarketRule) else DailyMarketRule(**r)
                 for s, r in rules.items()}
        prices = {s: state.valuation_prices[s] for s in symbols}
        plan = plan_rebalance(target, cash=state.available_cash, holdings=state.positions,
            sellable=state.sellable, prices=prices, rules=rules,
            valuation_at=state.as_of, decision_at=state.as_of, cash_reserve=D(0),
            receivables=state.receivables)
        intents = [replace(i, intent_id=f'{day}:{i.symbol}:{i.side}') for i in plan.intents]
        assert all(stamp <= state.as_of for stamp in state.valuation_times.values())
        decisions.append(dict(date=str(day), decision_time=state.as_of.isoformat(), execution_date=str(days[index + 1]),
            cash=str(state.available_cash), holdings=dict(state.positions),
            sellable=dict(state.sellable), nav=str(state.nav), weights=weights,
            actual_weights={s: str(D(q) * state.valuation_prices[s] / state.nav)
                            for s, q in state.positions.items()},
            weight_deviation={s: str(D(state.positions.get(s, 0)) * state.valuation_prices[s]
                                    / state.nav - D(str(weights.get(s, 0)))) for s in symbols},
            valuation_times={s: str(state.valuation_times[s]) for s in symbols},
            allocations=[asdict(a) for a in plan.allocations],
            intents=[asdict(i) for i in intents]))
        return intents

    prepared = prepared or prepare_backtest(data.instruments, data.bars, data.rules,
        trading_days=days, corporate_actions=data.corporate_actions)
    result = run_prepared_backtest(prepared, portfolio_signal=signal,
        initial_cash=initial, participation_rate=participation, slippage_bps=slippage,
        strategy_id='weekly-ma20' if filtered else 'weekly-equal-weight',
        strategy_params={'rebalance_sessions': 5, 'window': 20, 'policy': 'one submission; no chase'},
        data_snapshot_id=data.snapshot['snapshot_id'])
    check_cash(result, initial)
    return result, decisions


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / 'summary.json').unlink(missing_ok=True)
    started = time.perf_counter()
    data = load_tushare_lake(args.root, SYMBOLS, '2025-01-02', '2025-06-30')
    loaded = time.perf_counter()
    prepared = prepare_backtest(data.instruments, data.bars, data.rules,
        trading_days=data.trading_days, corporate_actions=data.corporate_actions)
    ready = time.perf_counter()
    report = dict(status='ENGINEERING_ACCEPTANCE_NOT_ALPHA', snapshot=data.snapshot,
        strategy_source='user-authorized fixed local candidate; not a ResearchPod live-model output',
        source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        admission=dict(historical_universe=False, historical_vintage_pit=False,
            price='raw', volume='shares', signal='closed bars only; weekly MA20',
            universe='explicit event-coverage pool; not historical market selection',
            model='O-H-L-C synthetic book; ex-post bar volume; not observed L2',
            unfilled='one submission; no chase; replan from actual account next fifth session',
            blocked_claims=['investment effectiveness', 'all-market unbiased selection',
                            'strict vintage PIT', 'observed intrabar execution', 'live readiness']),
        counts=dict(bars=len(data.bars), sessions=len(data.trading_days),
                    actions=len(data.corporate_actions)), runs=[])
    reference = None
    opening = {(b.symbol, b.start.date()): D(str(b.open)) for b in data.bars}
    for name, filtered, participation, slip in [
        ('candidate', True, .01, 0), ('benchmark', False, .01, 0),
        ('low-capacity', True, .000001, 0), ('adverse-slippage', True, .01, 10),
        ('repeat', True, .01, 0)]:
        tick = time.perf_counter()
        result, decisions = run_candidate(data, filtered, D(1000000), participation, slip, prepared)
        replayed = time.perf_counter()
        assert len(decisions) == 24
        assert not result.pending_intents
        nav = {r['date']: r for r in result.daily_nav}
        if slip == 0:
            assert all(D(f['price']) == opening[(f['symbol'], datetime.fromtimestamp(
                f['time']/1e9, ZoneInfo('Asia/Shanghai')).date())] for f in result.fills)
        expected_dividends = {}
        for action in data.corporate_actions:
            quantity = D(nav[str(action.record_date)]['holdings'].get(action.symbol, 0))
            expected = (quantity * action.cash_div).quantize(D('.01'), rounding=ROUND_HALF_UP)
            actual = sum((D(r['amount']) for r in result.account_journal
                          if r['event_id'].startswith(f'{action.symbol}|{action.ex_date}|')
                          and r['event_id'].endswith('|dividend')), D(0))
            assert actual == expected
            expected_dividends[action.symbol] = str(expected)
        if name == 'candidate':
            assert any(r['reason'] == 'INSUFFICIENT_CASH_WITH_FEES' for r in result.rejections)
        if name == 'benchmark':
            frozen = [r for r in result.daily_nav if '2025-05-06' <= r['date'] <= '2025-05-16']
            assert len(frozen) == 9
            assert {D(r['holdings']['002128.SZ']) for r in frozen} == {D(13600)}
            assert not any(f['symbol'] == '002128.SZ' and
                           '2025-05-06' <= datetime.fromtimestamp(
                               f['time']/1e9, ZoneInfo('Asia/Shanghai')).date().isoformat()
                           <= '2025-05-16' for f in result.fills)
        if name == 'low-capacity':
            assert not result.fills and result.native_cash == D(1000000)
        serialized = result.to_dict()
        if name == 'candidate':
            reference = result
        if name == 'repeat':
            report['repeat_comparison'] = compare_complete_backtests(reference, result)
            assert report['repeat_comparison']['equal']
        outcomes = execution_outcomes(decisions, serialized)
        artifact = args.output / f'{name}.json'
        artifact.write_text(json.dumps(
            dict(result=serialized, decisions=decisions, execution_outcomes=outcomes),
            default=str, ensure_ascii=False, indent=2), encoding='utf-8')
        report['runs'].append(dict(name=name, fills=len(result.fills), rejections=len(result.rejections),
            decisions=len(decisions), journal_entries=len(result.account_journal),
            independently_expected_dividends=expected_dividends,
            execution_outcomes=dict(Counter(row['outcome'] for row in outcomes)),
            manifest=result.manifest,
            artifact_sha256=hashlib.sha256(artifact.read_bytes()).hexdigest(),
            replay_seconds=replayed-tick,
            final_equity=result.daily_nav[-1]['total_equity'], seconds=time.perf_counter()-tick,
            rejection_reasons=sorted({r['reason'] for r in result.rejections})))
        print(name, {k: v for k, v in report['runs'][-1].items() if k != 'manifest'}, flush=True)
    report['timing'] = dict(load_seconds=loaded-started, prepare_seconds=ready-loaded,
                           total_seconds=time.perf_counter()-started,
                           scope='load through five replay/result exports; excludes interpreter/import startup')
    report['checks'] = ['native/gateway/ledger daily reconciliation', 'independent fill cash arithmetic',
        'record-date quantity times declared real dividend', 'nine halted holding sessions without fills',
        'capacity rejection', 'actual cash shortage', 'fresh-account repeat complete equality']
    report['path_sensitivity'] = ('This use case submits IOC market intents only; every zero-slippage fill '
        'was independently checked against its raw next-session opening price. No resting-limit '
        'path comparison or true intrabar ordering is claimed. Ex-post volume remains an assumption.')
    (args.output / 'summary.json').write_text(json.dumps(report, default=str, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
