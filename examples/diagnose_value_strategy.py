"""Read-only accounting/exposure diagnosis of the frozen V/R30 validation run."""
import argparse
from collections import Counter
from decimal import Decimal as D
import json
from pathlib import Path
import sys
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import compare_backtests
from examples.value_data_snapshot import sha, check_output_location


def money(value):
    return format(value, '.2f')


def pnl_bridge(result, initial_cash):
    last = result['daily_nav'][-1]
    fees = sum((D(f['fee']) for f in result['fills']), D(0))
    principal = sum((D(f['quantity'])*D(f['price'])*(1 if f['side']=='SELL' else -1)
                     for f in result['fills']), D(0))
    journal = result['account_journal']
    dividends = sum((D(r['amount']) for r in journal if r['event_id'].endswith('|dividend')), D(0))
    fractional = sum((D(r['amount']) for r in journal if r['event_id'].endswith('|fraction')), D(0))
    other = sum((D(r['amount']) for r in journal), D(0))-dividends-fractional
    trade_mark = principal+D(last['positions_value'])
    receivables = D(last['receivable_dividends'])
    net = D(last['total_equity'])-D(initial_cash)
    if trade_mark+dividends+fractional+other+receivables-fees != net:
        raise ValueError('P&L bridge does not reconcile exactly')
    return {key: money(value) for key, value in dict(net_pnl=net, trade_and_ending_value=trade_mark,
        paid_dividends=dividends, fractional_cash=fractional, other_cash=other,
        ending_receivables=receivables, paid_fees=fees,
        net_pnl_plus_recorded_fees=net+fees).items()}


def residual_tag(planning_reasons, outcome, decision_quantity, overnight_ratios, execution_quantity):
    if planning_reasons:
        return 'PLANNING:'+'+'.join(sorted(planning_reasons))
    if outcome is None:
        return 'UNCLASSIFIED'
    if outcome['rejection_reasons']:
        return 'EXECUTION:'+'+'.join(sorted(outcome['rejection_reasons']))
    if outcome['outcome'] == 'PARTIALLY_FILLED':
        return 'EXECUTION:PARTIALLY_FILLED'
    if outcome['outcome'] == 'FILLED' and overnight_ratios:
        expanded = D(decision_quantity)
        for ratio in overnight_ratios:
            expanded = D(int(expanded*D(ratio)))
        residual = expanded-D(outcome['filled_quantity'])
        if residual > 0 and residual == D(execution_quantity):
            return 'OVERNIGHT_SHARE_ACTION_AFTER_SIZING'
    return 'UNCLASSIFIED'


def target_diagnostics(artifact, actions):
    result = artifact['result']
    nav = [r for r in result['daily_nav'] if '2021-01-01' <= r['date'] <= '2023-12-31']
    by_day = {r['date']: r for r in nav}
    decisions = sorted(artifact['decisions'], key=lambda d: d['execution_date'])
    if any(not isinstance(d['weights'], dict) for d in decisions):
        raise ValueError('Structured target-weight archive required')
    outcomes = {(r['decision_time'], r['symbol'], r['side']): r for r in artifact['execution_outcomes']}
    counts, reasons, details = Counter(), Counter(), []
    current = -1
    for row in nav:
        while current+1 < len(decisions) and decisions[current+1]['execution_date'] <= row['date']:
            current += 1
        if current < 0:
            continue
        decision = decisions[current]
        held = {s for s, q in row['holdings'].items() if D(q)>0}
        target = {s for s, w in decision['weights'].items() if D(str(w))>0}
        extra = held-target
        counts['days_above_target_count'] += len(held)>len(target)
        counts['days_with_non_target_holdings'] += bool(extra)
        counts['non_target_holding_days'] += len(extra)
        allocations = {a['symbol']: a for a in decision['allocations']}
        for symbol in sorted(extra):
            ratios = [str(ratio) for s, day, ratio in actions if s==symbol and
                      decision['date'] < day <= decision['execution_date'] and ratio>1]
            outcome = outcomes.get((decision['decision_time'], symbol, 'SELL'))
            tag = residual_tag(allocations.get(symbol, {}).get('reasons', []), outcome,
                decision['holdings'].get(symbol, 0), ratios,
                by_day[decision['execution_date']]['holdings'].get(symbol, 0))
            reasons[tag] += 1
            details.append(dict(date=row['date'], symbol=symbol, quantity=row['holdings'][symbol],
                target_execution_date=decision['execution_date'], tag=tag))
    if not nav:
        raise ValueError('No validation NAV observations')
    counts['max_positions'] = max(sum(D(q)>0 for q in r['holdings'].values()) for r in nav)
    counts['mean_daily_cash_percent'] = str((sum(D(r['cash'])/D(r['total_equity']) for r in nav)/len(nav)*100).quantize(D('.0001')))
    return dict(counts=counts, non_target_holding_day_tags=reasons), details


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output = check_output_location(args.output, args.run)
    source_file = args.run/'summary.json'
    source_sha = sha(source_file)
    source = json.loads(source_file.read_text(encoding='utf-8'))
    if source['holdout_evaluated'] or not source['status'].startswith('PASS_EXECUTION_ADAPTATION'):
        raise ValueError('Require the frozen validation-only successful integration')
    artifacts, input_hashes = {}, {}
    for name in ('V', 'R30'):
        path = args.run/f'{name}.json'
        expected = next(r['artifact_sha256'] for r in source['runs'] if r['name']==name)
        if sha(path) != expected:
            raise ValueError(f'Frozen artifact hash mismatch: {name}')
        artifacts[name] = json.loads(path.read_text(encoding='utf-8'))
        if any(not '2020-12-01' <= r['date'] <= '2023-12-31' for r in artifacts[name]['result']['daily_nav']):
            raise ValueError('Only frozen validation/warmup NAV observations are supported')
        if sha(path) != expected:
            raise ValueError(f'Artifact changed while reading: {name}')
        input_hashes[name] = expected
    if not compare_backtests(artifacts['V']['result'], artifacts['R30']['result'])['comparable_model']:
        raise ValueError('Economic models must match')
    action_file = args.run/'derived_lake/lake/actions/dividend.parquet'
    expected_action = source['derived_snapshot']['derived_files_sha256']['lake/actions/dividend.parquet']
    if sha(action_file) != expected_action:
        raise ValueError('Frozen action input hash mismatch')
    rows = pq.read_table(action_file, columns=['ts_code','ex_date','div_proc','stk_bo_rate','stk_co_rate']).to_pylist()
    actions = sorted({(r['ts_code'], r['ex_date'][:4]+'-'+r['ex_date'][4:6]+'-'+r['ex_date'][6:],
        D(1)+D(str(r['stk_bo_rate'] or 0))+D(str(r['stk_co_rate'] or 0)))
        for r in rows if r['div_proc']=='实施' and r.get('ex_date')})
    report = dict(source_summary_sha256=source_sha, source_artifact_sha256=input_hashes,
        action_input_sha256=expected_action, script_sha256=sha(__file__), holdout_evaluated=False,
        analysis='Accounting decomposition and observed target residuals; not causal return attribution', cases={})
    private = {}
    for name, artifact in artifacts.items():
        result = artifact['result']
        bridge = pnl_bridge(result, result['manifest']['config']['initial_cash'])
        targets, private[name] = target_diagnostics(artifact, actions)
        report['cases'][name] = dict(pnl_bridge=bridge, target_diagnostics=targets)
    left, right = (report['cases'][n]['pnl_bridge'] for n in ('V','R30'))
    report['R30_minus_V'] = {key: money(D(right[key])-D(left[key])) for key in left}
    report['observed_paid_fee_contribution_to_gap'] = money(D(left['paid_fees'])-D(right['paid_fees']))
    report['status'] = ('RECONCILED_WITH_UNCLASSIFIED_RESIDUALS' if any(
        c['target_diagnostics']['non_target_holding_day_tags'].get('UNCLASSIFIED', 0) for c in report['cases'].values())
        else 'RECONCILED_AND_OBSERVED_RESIDUALS_EXPLAINED')
    if sha(source_file)!=source_sha or sha(action_file)!=expected_action:
        raise ValueError('Frozen inputs changed during analysis')
    args.output.mkdir(parents=True, exist_ok=True)
    details_file = args.output/'private_details.json'
    details_file.write_text(json.dumps(private, ensure_ascii=False, indent=2), encoding='utf-8')
    report['private_details_sha256'] = sha(details_file)
    (args.output/'summary.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
