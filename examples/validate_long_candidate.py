"""Fixed 20-stock/five-year account-feedback use check; no alpha claims."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from decimal import Decimal as D, ROUND_HALF_UP
from collections import Counter
import argparse
import hashlib
import json
import time
from engine import prepare_backtest
from engine.tushare_lake import load_tushare_lake
from examples.research_workflow import run_candidate, execution_outcomes
from examples.benchmark_dense_lake import _peak_mib
from engine import compare_complete_backtests

SYMBOLS_LONG = ['000001.SZ','000002.SZ','000063.SZ','000333.SZ','000651.SZ','000858.SZ',
    '002027.SZ','002415.SZ','002475.SZ','002594.SZ','600000.SH','600009.SH','600016.SH',
    '600028.SH','600030.SH','600036.SH','600048.SH','600050.SH','600104.SH','600519.SH']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output
    output.mkdir(parents=True, exist_ok=True)
    (output/'summary.json').unlink(missing_ok=True)
    report = dict(status='RUNNING', symbols=SYMBOLS_LONG, start='2021-01-04', end='2025-12-31',
        purpose='account-feedback engineering use; fixed prior 20-stock universe; not unbiased alpha research',
        historical_vintage_pit=False, initial_cash='1000000', participation_rate='.01',
        slippage_bps=0, fractional_policy='reject', runs=[])
    started = time.perf_counter()
    try:
        data = load_tushare_lake(args.root,
            SYMBOLS_LONG, report['start'], report['end'])
        loaded = time.perf_counter()
        report.update(snapshot=data.snapshot, bars=len(data.bars), sessions=len(data.trading_days),
                      actions=len(data.corporate_actions), load_seconds=loaded-started,
                      share_actions=sum(a.bonus_ratio + a.reserve_ratio > 0 for a in data.corporate_actions))
        prepared = prepare_backtest(data.instruments,data.bars,data.rules,
            trading_days=data.trading_days,corporate_actions=data.corporate_actions)
        report['prepare_seconds'] = time.perf_counter()-loaded
        reference = None
        for name, filtered in [('candidate',True),('benchmark',False),('repeat',True)]:
            tick = time.perf_counter()
            result, decisions = run_candidate(data,filtered,D(1000000),.01,0,prepared)
            outcomes = execution_outcomes(decisions,result)
            nav = {row['date']: row for row in result.daily_nav}
            for action in data.corporate_actions:
                quantity = D(nav[str(action.record_date)]['holdings'].get(action.symbol, 0))
                expected = (quantity * action.cash_div).quantize(D('.01'), rounding=ROUND_HALF_UP)
                actual = sum((D(row['amount']) for row in result.account_journal
                    if row['event_id'].startswith(f'{action.symbol}|{action.ex_date}|')
                    and row['event_id'].endswith('|dividend')), D(0))
                assert actual == expected, (action.symbol, action.ex_date, actual, expected)
            assert len(decisions) == len([i for i in range(len(data.trading_days)-1) if i%5==0])
            assert not result.pending_intents
            assert all(len(d['weights']) <= len(SYMBOLS_LONG) for d in decisions)
            if name == 'candidate': reference = result
            if name == 'repeat':
                report['repeat_comparison'] = compare_complete_backtests(reference,result)
                assert report['repeat_comparison']['equal']
            artifact = output/(name+'.json')
            artifact.write_text(json.dumps(dict(result=result.to_dict(),decisions=decisions,
                execution_outcomes=outcomes),default=str,ensure_ascii=False,indent=2),encoding='utf-8')
            report['runs'].append(dict(name=name,seconds=time.perf_counter()-tick,
                decisions=len(decisions),fills=len(result.fills),rejections=len(result.rejections),
                outcomes=dict(Counter(r['outcome'] for r in outcomes)),
                rejection_reasons=dict(Counter(r['reason'] for r in result.rejections)),
                planning_reasons=dict(Counter(reason for d in decisions for a in d['allocations'] for reason in a['reasons'])),
                max_positions=max(sum(D(q)>0 for q in r['holdings'].values()) for r in result.daily_nav),
                nonzero_cash_events=sum(D(r['amount'])!=0 for r in result.account_journal),
                held_share_actions=sum(a.bonus_ratio + a.reserve_ratio > 0 and
                    D(nav[str(a.record_date)]['holdings'].get(a.symbol, 0)) > 0
                    for a in data.corporate_actions),
                artifact_sha256=hashlib.sha256(artifact.read_bytes()).hexdigest(),manifest=result.manifest))
            print(name,{k:v for k,v in report['runs'][-1].items() if k!='manifest'},flush=True)
        report['status']='PASS_ACCOUNT_FEEDBACK_NOT_ALPHA'
    except Exception as error:
        report.update(status='BLOCKED_OR_FAILED',error_type=type(error).__name__,reason=str(error))
        raise
    finally:
        report.update(total_seconds=time.perf_counter()-started,peak_working_set_mib=_peak_mib(),
            timing_scope='same process load, prepare, three replays/validations/exports; excludes interpreter/imports',
            workflow_sha256=hashlib.sha256(Path(sys.modules['examples.research_workflow'].__file__).read_bytes()).hexdigest(),
            experiment_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
        (output/'summary.json').write_text(json.dumps(report,default=str,ensure_ascii=False,indent=2),encoding='utf-8')


if __name__ == '__main__':
    main()
