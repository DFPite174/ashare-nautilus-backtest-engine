"""Measure one fresh-process NC30 replay using the frozen 2021–2023 account."""
import argparse
from datetime import datetime
from decimal import Decimal as D
from pathlib import Path
import sys
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import OrderIntent, TimedOrderIntent, compare_complete_backtests, prepare_order_tape, run_order_tape
from examples.benchmark_dense_lake import _peak_mib
from examples.integrate_value_strategy import replay
from examples.validate_nc30_execution_pressure import load_frozen_nc30, write
from examples.value_data_snapshot import check_output_location


def frozen_entries(decisions):
    for decision in decisions:
        decision_time = datetime.fromisoformat(decision['decision_time'])
        for row in decision['intents']:
            intent = dict(row)
            if intent['earliest_execution']:
                intent['earliest_execution'] = datetime.fromisoformat(intent['earliest_execution'])
            yield TimedOrderIntent(decision_time, OrderIntent(**intent))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--closeout', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--mode', required=True, choices=('feedback', 'orders'))
    args = parser.parse_args()
    root = args.closeout.resolve()
    out = check_output_location(args.output, root)
    if out.exists() and any(out.iterdir()):
        raise ValueError('Use a fresh output directory')
    out.mkdir(parents=True, exist_ok=True)

    started = perf_counter()
    data, targets, reference, bundle_sha, snapshot_sha = load_frozen_nc30(root)
    loaded = perf_counter()
    prepared = data.prepare()
    tape = None
    if args.mode == 'orders':
        tape = prepare_order_tape(prepared, frozen_entries(reference['decisions']))
    ready = perf_counter()
    if tape is None:
        result, _ = replay(data, prepared, targets, dict(bundle_sha256=bundle_sha))
    else:
        result = run_order_tape(prepared, tape, initial_cash=D(1000000), participation_rate=.01,
                                slippage_bps=10, data_snapshot_id=data.snapshot['snapshot_id'])
    executed = perf_counter()
    actual = result.to_dict()
    write(out/'account.json', actual)
    exported = perf_counter()
    peak = _peak_mib()
    comparison = compare_complete_backtests(reference['result'], actual)
    if not comparison['equal']:
        write(out/'comparison.json', comparison)
        raise RuntimeError('Replay differs from the frozen account; see comparison.json')
    write(out/'summary.json', dict(mode=args.mode, symbols=len(data.instruments),
        sessions=len(data.trading_days), bars=len(data.bars), actions=len(data.corporate_actions),
        intents=sum(len(d['intents']) for d in reference['decisions']), fills=len(result.fills),
        rejections=len(result.rejections), input_sha256=prepared.input_sha256, snapshot_sha256=snapshot_sha,
        target_bundle_sha256=bundle_sha, holdout_evaluated=False, result_matches_reference=True,
        load_seconds=loaded-started, prepare_seconds=ready-loaded, execution_seconds=executed-ready,
        export_seconds=exported-executed, total_seconds=exported-started, peak_process_mib=peak,
        statistics=result.run_statistics))


if __name__ == '__main__':
    main()
