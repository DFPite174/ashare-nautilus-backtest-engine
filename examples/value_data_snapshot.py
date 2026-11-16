"""Private derived lake for one value-strategy integration; originals stay immutable."""
from pathlib import Path
import hashlib
import json
import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq
from engine.reproducibility import digest


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b''):
            h.update(chunk)
    return h.hexdigest()


def check_output_location(destination, *protected):
    destination = Path(destination).resolve()
    for source in map(lambda p: Path(p).resolve(), protected):
        if source.is_relative_to(destination) or destination.is_relative_to(source):
            raise ValueError('Output must not overlap protected inputs')
    return destination


def resolve_actions(rows, resolutions):
    output = list(rows)
    audit = []
    seen = set()
    for rule in resolutions:
        key = rule['symbol'], rule['ex_date']
        if key in seen or not rule.get('evidence_url') or not rule.get('reason'):
            raise ValueError('Duplicate or undocumented action resolution')
        seen.add(key)
        group = [r for r in output if (r['ts_code'], r.get('ex_date')) == key]
        if sorted(digest(r) for r in group) != rule['expected_row_sha256']:
            raise ValueError(f'Action source fingerprint changed: {key}')
        canonical = rule['canonical']
        if (canonical['ts_code'], canonical['ex_date']) != key:
            raise ValueError('Canonical action identity mismatch')
        output = [r for r in output if (r['ts_code'], r.get('ex_date')) != key]
        output.append(dict(canonical))
        audit.append(dict(symbol=key[0], ex_date=key[1], input_sha256=rule['expected_row_sha256'],
            output_sha256=digest(canonical), evidence_url=rule['evidence_url'], reason=rule['reason']))
    return output, audit


def build_snapshot(root, destination, symbols, start, end, resolution_file):
    root = Path(root).resolve()
    destination = check_output_location(destination, root)
    resolutions = json.loads(Path(resolution_file).read_text(encoding='utf-8'))['resolutions']
    report = dict(source_files_sha256={}, derived_files_sha256={}, resolutions_sha256=sha(resolution_file),
                  symbols=sorted(symbols), start=start, end=end)
    codes = ds.field('ts_code').isin(symbols)
    span = (ds.field('trade_date') >= start.replace('-', '')) & (ds.field('trade_date') <= end.replace('-', ''))
    tables = [('meta/stock_basic.parquet', codes), ('meta/trade_cal.parquet', None),
              ('actions/dividend.parquet', codes)]
    for year in range(int(start[:4]), int(end[:4])+1):
        for name in ('daily', 'stk_limit', 'suspend_d'):
            paths = sorted((root/'lake'/f'market/{name}/year={year}').glob('*.parquet'))
            if not paths:
                raise FileNotFoundError(f'Missing original {name} {year}')
            tables.extend((p.relative_to(root/'lake').as_posix(), codes & span) for p in paths)
    for relative, predicate in tables:
        source, target = root/'lake'/relative, destination/'lake'/relative
        before = sha(source)
        table = ds.dataset(source, format='parquet').to_table(filter=predicate)
        if relative == 'actions/dividend.parquet':
            rows, report['action_resolution_audit'] = resolve_actions(table.to_pylist(), resolutions)
            table = pa.Table.from_pylist(rows, schema=table.schema)
        if sha(source) != before:
            raise ValueError(f'Original file changed during snapshot: {relative}')
        target.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(table, target)
        report['source_files_sha256']['lake/'+relative] = before
        report['derived_files_sha256']['lake/'+relative] = sha(target)
    report['snapshot_sha256'] = digest(report)
    (destination/'snapshot_manifest.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report
