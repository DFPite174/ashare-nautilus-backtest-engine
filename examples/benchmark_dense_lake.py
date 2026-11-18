"""Bounded engineering comparison using the read-only local Tushare lake."""
import argparse
from decimal import Decimal
import gc
import hashlib
import json
from pathlib import Path
from statistics import median
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import (OrderIntent, TimedOrderIntent, compare_complete_backtests,
                    prepare_order_tape, run_order_tape, run_prepared_backtest)
from engine.backtest import at
from engine.tushare_lake import load_tushare_lake, parse_day
from examples.validate_tushare_lake import check_cash


def build_entries(prepared, width):
    if type(width) is not int or not 0 < width <= len(prepared.master):
        raise ValueError('Width must fit the prepared universe')
    if len(prepared.sessions) < 2 or width * (len(prepared.sessions) - 1) > 20000:
        raise ValueError('Require at least two sessions and at most 20000 intents')
    symbols = sorted(prepared.master)[:width]
    return [TimedOrderIntent(at(day, 15), OrderIntent(symbol, 'BUY' if n % 2 == 0 else 'SELL',
                prepared.master[symbol].min_order_qty, intent_id=f'dense-{width}:{n}:{symbol}'))
            for n, day in enumerate(prepared.sessions[:-1]) for symbol in symbols]


def compare_complete(reference, candidate):
    return compare_complete_backtests(reference, candidate)


def _peak_mib():
    try:
        import psutil
    except ImportError:
        return None
    peak = getattr(psutil.Process().memory_info(), 'peak_wset', None)
    return None if peak is None else peak / 1024 ** 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--universe', type=Path, required=True, help='JSON list or object with symbols')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--width', type=int, choices=(64, 128), required=True)
    parser.add_argument('--start', default='2021-01-04')
    parser.add_argument('--sessions', type=int, default=126)
    parser.add_argument('--repeats', type=int, default=3)
    args = parser.parse_args()
    if not 2 <= args.sessions <= 126 or not 1 <= args.repeats <= 3:
        parser.error('Require 2..126 sessions and 1..3 repeats')
    args.output.mkdir(parents=True, exist_ok=True)
    report = dict(status='RUNNING', phase='loading', samples=[], width=args.width,
                  runner_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                  limits=['Engineering quantities, not alpha or a historical selection universe',
                          'Default reject fractional policy; no silent exclusions or data repair',
                          'Peak is cumulative shared-process memory, not a per-mode measurement',
                          'Prepared execution timings exclude load/prepare/compare/JSON output'])
    def save():
        (args.output / 'summary.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    save()
    try:
        universe = json.loads(args.universe.read_text(encoding='utf-8'))
        symbols = universe['symbols'] if isinstance(universe, dict) else universe
        if not isinstance(symbols, list) or any(type(s) is not str for s in symbols) or len(set(symbols)) != len(symbols):
            raise ValueError('Explicit unique symbol list required')
        import pyarrow.dataset as ds
        start = parse_day(args.start)
        calendar = ds.dataset(str(args.root / 'lake/meta/trade_cal.parquet'), format='parquet').to_table(
            columns=['exchange', 'cal_date', 'is_open']).to_pylist()
        dates = sorted(r['cal_date'] for r in calendar if r['exchange'] == 'SSE'
                       and r['is_open'] == 1 and r['cal_date'] >= start.strftime('%Y%m%d'))
        if len(dates) < args.sessions:
            raise ValueError('Insufficient official sessions')
        end = parse_day(dates[args.sessions - 1])
        begin = time.perf_counter()
        data = load_tushare_lake(args.root, symbols, start.isoformat(), end.isoformat())
        report.update(load_seconds=time.perf_counter() - begin, snapshot=data.snapshot,
                      start=start.isoformat(), end=end.isoformat(), symbols=sorted(symbols),
                      bars=len(data.bars), actions=len(data.corporate_actions), phase='preparing')
        save()
        begin = time.perf_counter()
        prepared = data.prepare()
        report['prepare_seconds'] = time.perf_counter() - begin
        if len(prepared.sessions) != args.sessions:
            raise ValueError('Prepared session count differs from requested official sessions')
        entries = build_entries(prepared, args.width)
        tape = prepare_order_tape(prepared, entries)
        report.update(input_sha256=prepared.input_sha256, tape_sha256=tape.sha256,
                      sessions=len(prepared.sessions), intents=len(entries), phase='replaying')
        by_day = {day: [e.intent for e in rows] for day, rows in tape.by_day.items()}
        def signal(closed, state):
            return by_day.get(closed[0].end.date(), ())
        options = dict(initial_cash=Decimal(100000000), participation_rate=0.01,
                       slippage_bps=0, strategy_id=f'dense-lake-{args.width}-v1',
                       data_snapshot_id=data.snapshot['snapshot_id'])
        reference = None
        modes = ('full', 'sparse', 'orders')
        save()
        for trial in range(args.repeats):
            sequence = modes[trial:] + modes[:trial]
            for mode in sequence:
                gc.collect()
                begin = time.perf_counter()
                result = (run_order_tape(prepared, tape, **options) if mode == 'orders'
                          else run_prepared_backtest(prepared, portfolio_signal=signal, data_mode=mode, **options))
                seconds = time.perf_counter() - begin
                check_cash(result, options['initial_cash'])
                value = result.to_dict()
                if reference is None:
                    reference = value
                comparison = compare_complete(reference, value)
                filename = f'{mode}-{trial + 1}.json'
                (args.output / filename).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
                row = dict(mode=mode, trial=trial + 1, seconds=seconds, fills=len(result.fills),
                           rejections=len(result.rejections), final_cash=str(result.native_cash),
                           max_holdings=max(sum(Decimal(q) > 0 for q in nav['holdings'].values()) for nav in result.daily_nav),
                           file=filename, comparison=comparison, statistics=result.run_statistics,
                           peak_process_mib=_peak_mib())
                report['samples'].append(row)
                save()
                print(json.dumps({k: v for k, v in row.items() if k not in ('comparison',)}, ensure_ascii=False), flush=True)
                if any(r['reason'].startswith('Exceeded MAX_ORDER_SUBMIT_RATE') for r in result.rejections):
                    raise RuntimeError('Native submit-rate guard interfered with the bounded workload')
                if not comparison['equal']:
                    raise RuntimeError(f'Business mismatch: {mode} trial {trial + 1}')
                del result, value
        report.update(status='OK', phase='complete', medians={mode: median(
            r['seconds'] for r in report['samples'] if r['mode'] == mode) for mode in modes})
        save()
    except Exception as exc:
        report.update(status='FAILED', error=f'{type(exc).__name__}: {exc}')
        save()
        raise


if __name__ == '__main__':
    main()
