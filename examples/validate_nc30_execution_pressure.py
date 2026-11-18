"""Replay the four fixed NC30 execution assumptions on the frozen validation account."""
import argparse
from collections import Counter, defaultdict
from decimal import Decimal as D
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from contracts.models import TargetWeights
from engine.company_action_admission import load_company_action_facts
from engine.reproducibility import digest
from engine.tushare_lake import load_tushare_lake
from examples.closeout_data import apply_declared_lifecycle_rules
from examples.integrate_value_strategy import replay
from examples.research_workflow import execution_outcomes
from examples.run_value_p1 import economic_path
from examples.value_data_snapshot import check_output_location, sha


SCENARIOS = {
    'baseline': (.01, 10),
    'capacity': (.001, 10),
    'price': (.01, 30),
    'combined': (.001, 30),
}


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def write(path, value):
    path.write_text(json.dumps(value, default=str, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def summarize(result, decisions, outcomes):
    nav = {row['date']: row for row in result.daily_nav}
    rows = [row for row in result.daily_nav if row['date'].startswith('2023-')]
    prior = D(nav[max(day for day in nav if day < '2023')]['total_equity'])
    peak, drawdown = prior, D(0)
    for row in rows:
        equity = D(row['total_equity'])
        peak = max(peak, equity)
        drawdown = min(drawdown, equity / peak - 1)
    orders = defaultdict(list)
    for outcome in outcomes:
        orders[outcome['execution_date']].append(outcome)
    months, reasons = [], Counter()
    for decision in decisions:
        day = decision['execution_date']
        if not day.startswith('2023-'):
            continue
        weights = decision['weights']
        row = nav[day]
        cash = D(row['cash'])
        outside = sorted(s for s, q in row['holdings'].items() if D(str(q)) and s not in weights)
        feedback = sorted(s for s, q in decision['holdings'].items() if D(str(q)) and s not in weights)
        unfilled = []
        for outcome in orders[day]:
            quantity = D(outcome['unfilled_quantity'])
            if quantity:
                symbol = outcome['symbol']
                amount = quantity * decision['decision_prices'][symbol]
                rejection = outcome['rejection_reasons']
                reasons.update(rejection)
                unfilled.append(dict(symbol=symbol, side=outcome['side'], quantity=quantity,
                                     amount=amount, reasons=rejection))
        months.append(dict(execution_date=day, intents=len(orders[day]), fully_filled=sum(
            o['outcome']=='FILLED' for o in orders[day]), unfilled=unfilled,
            unfilled_value=sum((o['amount'] for o in unfilled), D(0)),
            cash=cash, cash_weight=cash / D(row['total_equity']), equity=D(row['total_equity']),
            outside_targets=outside, holdings_outside_next_targets_at_decision=feedback))
    assert len(rows) and len(months)==12
    final = rows[-1]
    return dict(start_equity=prior, end_equity=D(final['total_equity']), return_2023=D(final['total_equity'])/prior-1,
                max_drawdown_2023=drawdown, intents=sum(m['intents'] for m in months),
                fully_filled=sum(m['fully_filled'] for m in months), rejection_reasons=dict(reasons),
                unfilled_value=sum((m['unfilled_value'] for m in months), D(0)),
                max_post_trade_cash_weight=max(m['cash_weight'] for m in months),
                fees_2023=sum((D(o['fees']) for o in outcomes if o['execution_date'].startswith('2023-')), D(0)),
                months=months)


def load_frozen_nc30(root):
    prior = read(root/'summary.json')
    bundle = read(root/'frozen_targets.json')
    targets = [TargetWeights(**r) for r in bundle['validation']['NC30']]
    if (prior['holdout_evaluated'] or digest(bundle)!=prior['target_bundle_sha256']
            or len(targets)!=36 or any(not 2021<=t.execution_date.year<=2023 for t in targets)
            or any(t.signal_date.year>2023 for t in targets)):
        raise ValueError('Expected 36 frozen NC30 targets with final holdout sealed')
    lake = root/'validation/derived_lake'
    snapshot = read(lake/'snapshot_manifest.json')
    frozen_run = next(r for r in prior['phases']['validation']['runs'] if r['name']=='NC30')
    baseline = root/'validation/NC30.json'
    if (snapshot['snapshot_sha256']!=prior['phases']['validation']['snapshot_sha256']
            or digest({k:v for k,v in snapshot.items() if k!='snapshot_sha256'})!=snapshot['snapshot_sha256']
            or any(sha(lake/file)!=expected for file,expected in snapshot['derived_files_sha256'].items())
            or sha(baseline)!=frozen_run['artifact_sha256']):
        raise ValueError('Frozen market snapshot or reference account changed')
    reference = read(baseline)
    symbols = sorted({symbol for target in targets for symbol in target.weights})
    data = load_tushare_lake(lake, symbols, '2020-12-01', '2023-12-29')
    apply_declared_lifecycle_rules(data, load_company_action_facts())
    data.corporate_actions = [a.model_copy(update={'fractional_policy':'cash_in_lieu'})
                              if a.bonus_ratio+a.reserve_ratio>0 else a for a in data.corporate_actions]
    return data, targets, reference, prior['target_bundle_sha256'], snapshot['snapshot_sha256']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--closeout', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    root = args.closeout.resolve()
    out = check_output_location(args.output, root)
    if out.exists() and any(out.iterdir()):
        raise ValueError('Use a fresh output directory')
    data, targets, reference, bundle_sha, snapshot_sha = load_frozen_nc30(root)
    prepared = data.prepare()
    out.mkdir(parents=True, exist_ok=True)
    report = dict(target_bundle_sha256=bundle_sha, snapshot_sha256=snapshot_sha,
                  decisions=len(targets), symbols=len(data.instruments), holdout_evaluated=False, scenarios={})
    for name, (participation, slippage) in SCENARIOS.items():
        result, decisions = replay(data, prepared, targets, dict(bundle_sha256=bundle_sha),
            participation_rate=participation, slippage_bps=slippage, record_prices=True)
        actual = result.to_dict()
        if name=='baseline':
            old_decisions = [{k:v for k,v in d.items() if k!='decision_prices'} for d in decisions]
            if (economic_path(actual)!=economic_path(reference['result'])
                    or json.loads(json.dumps(old_decisions, default=str))!=reference['decisions']):
                raise ValueError('Baseline differs from frozen NC30 account')
        outcomes = execution_outcomes(decisions, result)
        metrics = summarize(result, decisions, outcomes)
        write(out/f'{name}.json', dict(result=actual, decisions=decisions, execution_outcomes=outcomes))
        report['scenarios'][name] = dict(participation_rate=participation, slippage_bps=slippage, **metrics)
        print(name, '2023 return', metrics['return_2023'], 'unfilled value', metrics['unfilled_value'], flush=True)
    write(out/'summary.json', report)


if __name__ == '__main__':
    main()
