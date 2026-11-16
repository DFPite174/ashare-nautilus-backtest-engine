"""Three fresh-process measurements of the fixed local-data workflow.

Uses the existing optional psutil peak-working-set probe; missing measurements
are reported as incomplete, never interpreted as zero memory usage.
"""
import argparse
import hashlib
import json
from pathlib import Path
from statistics import median
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
CHILD = """
import json, runpy, sys
from pathlib import Path
root, output = sys.argv[1:]
sys.argv = ['research_workflow.py', '--root', root, '--output', output]
runpy.run_path('examples/research_workflow.py', run_name='__main__')
from examples.benchmark_dense_lake import _peak_mib
Path(output, 'process-peak.json').write_text(json.dumps({'peak_working_set_mib': _peak_mib()}), encoding='utf-8')
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    destination = args.output / 'measurement.json'
    destination.unlink(missing_ok=True)
    trials = []
    for index in range(3):
        output = args.output / f'trial{index + 1}'
        started = time.perf_counter()
        child = subprocess.run([sys.executable, '-c', CHILD, str(args.root.resolve()), str(output)],
                               cwd=ROOT, capture_output=True, text=True)
        wall = time.perf_counter() - started
        if child.returncode:
            raise RuntimeError(f'Trial {index + 1} failed: {child.stderr}')
        summary = json.loads((output / 'summary.json').read_text(encoding='utf-8'))
        peak = json.loads((output / 'process-peak.json').read_text(encoding='utf-8'))
        assert len(summary['runs']) == 5 and summary['repeat_comparison']['equal']
        for run in summary['runs']:
            artifact = output / f'{run["name"]}.json'
            assert hashlib.sha256(artifact.read_bytes()).hexdigest() == run['artifact_sha256']
        trials.append(dict(trial=index + 1, process_seconds=wall, **peak,
            stages=summary['timing'], replay_seconds=sum(r['replay_seconds'] for r in summary['runs']),
            snapshot_id=summary['snapshot']['snapshot_id']))
        print(trials[-1], flush=True)
    sys.path.insert(0, str(ROOT))
    from engine import compare_complete_backtests
    for index in (2, 3):
        for name in ('candidate', 'benchmark', 'low-capacity', 'adverse-slippage', 'repeat'):
            left = json.loads((args.output / 'trial1' / f'{name}.json').read_text(encoding='utf-8'))
            right = json.loads((args.output / f'trial{index}' / f'{name}.json').read_text(encoding='utf-8'))
            assert compare_complete_backtests(left['result'], right['result'])['equal']
            assert left['decisions'] == right['decisions']
            assert left['execution_outcomes'] == right['execution_outcomes']
    complete = all(t['peak_working_set_mib'] is not None for t in trials)
    report = dict(status='PASS' if complete else 'MEASUREMENT_INCOMPLETE', trials=trials,
        workflow_sha256=summary['source_sha256'],
        benchmark_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        engine_source_sha256=summary['runs'][0]['manifest']['source_sha256'],
        environment=summary['runs'][0]['manifest']['environment'], workload=summary['counts'],
        process_seconds_median=median(t['process_seconds'] for t in trials),
        peak_working_set_mib_max=max(t['peak_working_set_mib'] for t in trials) if complete else None,
        scope='fresh interpreter/imports, local load, preparation, five replays, validation, exports and peak probe',
        cache='fresh processes; OS file cache not flushed; not disk-cold or five-year/all-market throughput',
        correctness='all five runs complete-equal across three processes; decisions/outcomes equal; artifact hashes verified')
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    if not complete:
        raise SystemExit('Memory measurement unavailable: existing optional psutil/Windows peak probe required')


if __name__ == '__main__':
    main()
