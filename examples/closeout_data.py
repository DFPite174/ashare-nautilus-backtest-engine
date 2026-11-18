"""Explicit historical data reconciliation; original lake and execution core stay unchanged."""
from collections import defaultdict
from datetime import date
from decimal import Decimal as D
import json
from pathlib import Path
import pyarrow as pa
import pyarrow.parquet as pq
from engine.reproducibility import digest
from examples.value_data_snapshot import resolve_actions, sha


def merge_identical_actions(rows):
    groups=defaultdict(list)
    for row in rows:
        groups[row['ts_code'],row.get('ex_date')].append(row)
    output=[];audit=[]
    for (symbol,ex),group in groups.items():
        economic=[{k:v for k,v in r.items() if k not in ('ann_date','imp_ann_date')} for r in group]
        if ex and len(group)>1 and len({digest(r) for r in economic})==1:
            selected=max(group,key=lambda r:r.get('imp_ann_date') or r.get('ann_date') or '')
            output.append(selected)
            audit.append(dict(symbol=symbol,ex_date=ex,source_row_hashes=[digest(r) for r in group],
                selected_sha256=digest(selected),policy='identical economics; latest reported implementation disclosure'))
        else: output.extend(group)
    return output,audit


def verify_zero_entitlements(nav,events):
    by_date={r['date']:r for r in nav}
    for event in events:
        day=event['record_date'];day=day[:4]+'-'+day[4:6]+'-'+day[6:]
        if day not in by_date: raise ValueError('Missing entitlement record snapshot')
        quantity=D(by_date[day]['holdings'].get(event['symbol'],0))
        if quantity:
            raise ValueError(f"Unsupported entitlement: {event['symbol']} {day} quantity={quantity}")


def reconcile_history(root,facts,start,end):
    root=Path(root);base=root/'lake'
    action_file=base/'actions/dividend.parquet';table=pq.read_table(action_file)
    rows=table.to_pylist();symbols={r['ts_code'] for r in rows}
    rows,resolved=resolve_actions(rows,[r for r in facts['resolutions'] if r['symbol'] in symbols])
    rows,duplicates=merge_identical_actions(rows)
    first,last=start.replace('-',''),end.replace('-','')
    declared=[r for r in facts['conditional_zero_entitlement'] if first<=r['record_date']<=last]
    # A public disclosure can establish an event even when the provider omits it.
    conditional=[r for r in declared if r['row_sha256'] is None]
    kept=[];excluded=[]
    by_hash={r['row_sha256']:r for r in declared if r['row_sha256'] is not None}
    no_entitlement={r['row_sha256']:r for r in facts.get('no_account_entitlement', [])
                    if first<=r['record_date']<=last}
    for row in rows:
        row_hash=digest(row)
        if row_hash in no_entitlement:
            excluded.append(no_entitlement[row_hash])
            continue
        rule=by_hash.get(row_hash)
        if rule:
            conditional.append(rule)
        else: kept.append(row)
    pq.write_table(pa.Table.from_pylist(kept,schema=table.schema),action_file)
    calendar=pq.read_table(base/'meta/trade_cal.parquet').to_pylist()
    days=sorted(r['cal_date'] for r in calendar if r['exchange']=='SSE' and r['is_open']==1)
    inserted=[]
    for file in sorted((base/'market/suspend_d').glob('year=*/*.parquet')):
        old=pq.read_table(file);susp=old.to_pylist();known={(r['ts_code'],r['trade_date']) for r in susp if r['suspend_type']=='S'}
        daily=next((base/'market/daily'/file.parent.name).glob('*.parquet'))
        quotes={(r['ts_code'],r['trade_date']) for r in pq.read_table(daily,columns=['ts_code','trade_date']).to_pylist()}
        year=file.parent.name.removeprefix('year=')
        for block in facts['listing_blocks']:
            for day in days:
                if not day.startswith(year) or not block['start']<=day<block['resume']: continue
                if (block['symbol'],day) in quotes: raise ValueError('Listing block conflicts with market quote')
                if (block['symbol'],day) in known: continue
                # S represents a declared non-trading session; retain the distinct listing reason.
                susp.append(dict(ts_code=block['symbol'],trade_date=day,suspend_timing=None,suspend_type='S',
                    date=day[:4]+'-'+day[4:6]+'-'+day[6:],source_kind='SUSPENDED_LISTING',evidence_url=block['start_url']))
                inserted.append((block['symbol'],day))
        schema=old.schema.append(pa.field('source_kind',pa.string())).append(pa.field('evidence_url',pa.string()))
        pq.write_table(pa.Table.from_pylist(susp,schema=schema),file)
    audit=dict(resolutions=resolved,identical_event_groups=duplicates,listing_block_sessions=inserted,
        conditional_zero_entitlement=conditional,no_account_entitlement=excluded)
    manifest_file=root/'snapshot_manifest.json'
    manifest=json.loads(manifest_file.read_text(encoding='utf-8'))
    for file in [action_file,*sorted((base/'market/suspend_d').glob('year=*/*.parquet'))]:
        manifest['derived_files_sha256'][file.relative_to(root).as_posix()]=sha(file)
    audit_file=root/'historical_reconciliation.json';audit_file.write_text(json.dumps(audit,ensure_ascii=False,indent=2),encoding='utf-8')
    manifest['historical_reconciliation_sha256']=sha(audit_file)
    manifest['snapshot_sha256']=digest({k:v for k,v in manifest.items() if k!='snapshot_sha256'})
    manifest_file.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    return manifest,conditional


def apply_declared_lifecycle_rules(data,facts):
    symbols={i.symbol for i in data.instruments};applied=[]
    for rule in data.rules.values():
        if rule['trade_status']!='OPEN' and rule.get('up_limit')==0 and rule.get('down_limit')==0:
            rule['limit_state']='UNAVAILABLE';rule.pop('up_limit');rule.pop('down_limit')
    for block in facts['listing_blocks']:
        day=block['resume'];key=block['symbol'],date.fromisoformat(day[:4]+'-'+day[4:6]+'-'+day[6:])
        if block['symbol'] not in symbols or key not in data.rules: continue
        rule=data.rules[key]
        rule['limit_state']='UNBOUNDED';rule.pop('up_limit',None);rule.pop('down_limit',None)
        applied.append(dict(symbol=block['symbol'],date=day,evidence_url=block['resume_url']))
    if applied:
        data.snapshot['declared_unbounded_restorations']=applied
        data.snapshot['snapshot_id']=digest({k:v for k,v in data.snapshot.items() if k!='snapshot_id'})
