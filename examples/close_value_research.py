"""Frozen NC30 cross-period closeout. Final holdout is never opened."""
import argparse
from collections import Counter
from datetime import date
from decimal import Decimal as D
import gc
import json
from pathlib import Path
import runpy
from statistics import median
import sys

import pandas as pd
import pyarrow.dataset as ds

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from contracts.models import TargetWeights
from engine.reproducibility import digest
from engine.tushare_lake import load_tushare_lake
from examples.run_value_p1 import treatment_frames, economic_path
from examples.run_value_industry_cap import capped_weights
from examples.integrate_value_strategy import replay
from examples.research_workflow import execution_outcomes
from examples.value_data_snapshot import sha, check_output_location, build_snapshot
from examples.closeout_data import reconcile_history, apply_declared_lifecycle_rules
from engine.company_action_admission import FACTS_PATH


def period_metrics(nav,first_day):
    rows=[r for r in nav if r['date']>=first_day]
    if not rows: raise ValueError('No evaluated account sessions')
    values=pd.Series([1000000.]+[float(r['total_equity']) for r in rows])
    returns=values.pct_change().dropna()
    return dict(start=rows[0]['date'],end=rows[-1]['date'],sessions=len(rows),
        total_return=float(values.iloc[-1]/1000000-1),annualized_return=float((values.iloc[-1]/1000000)**(252/len(rows))-1),
        max_drawdown=float((values/values.cummax()-1).min()),
        sharpe_zero_rf=float(returns.mean()/returns.std(ddof=1)*252**.5) if returns.std(ddof=1) else None)


def period_gate(candidate,controls):
    if candidate is None or len(controls)!=5:
        return dict(status='INCOMPLETE_DATA',**{'pass':False})
    ret=median(r['total_return'] for r in controls)
    draw=median(r['max_drawdown'] for r in controls)
    return dict(status='ASSESSED',control_median_return=ret,control_median_drawdown=draw,
                **{'pass':candidate['total_return']>ret and candidate['max_drawdown']>=draw})


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for flag in ('root','strategy-root','p1','cap','output'):
        parser.add_argument('--'+flag,type=Path,required=True)
    args=parser.parse_args()
    args.output=check_output_location(args.output,args.root,args.strategy_root,args.p1,args.cap)
    if args.output.exists() and any(args.output.iterdir()): raise ValueError('Use a fresh output directory')
    protected={}
    def read_json(path,expected=None):
        actual=sha(path)
        expected=expected or protected.get(path)
        if expected is not None and actual!=expected: raise ValueError('Frozen source changed: '+path.name)
        protected[path]=actual
        return json.loads(path.read_text(encoding='utf-8'))
    p1=read_json(args.p1/'summary.json');cap=read_json(args.cap/'summary.json')
    if p1['holdout_evaluated'] or cap['holdout_evaluated'] or cap['status']!='INDUSTRY_CAP_EXPERIMENT_COMPLETED':
        raise ValueError('Require successful sealed-holdout inputs')
    prior=read_json(args.p1/'target_provenance.json')
    repo=Path(__file__).resolve().parents[1]
    for filename,expected in prior['source_sha256'].items():
        path=Path(filename)
        if not path.is_absolute(): path=repo/path
        protected[path]=expected
        if sha(path)!=expected: raise ValueError('P1 input changed')
    functions={};src=args.strategy_root/'src/value_quant'
    for filename in ('portfolio.py','history_factors.py'):
        path=src/filename
        if str(path) not in prior['source_sha256']: raise ValueError('Strategy root differs')
        functions.update(runpy.run_path(str(path),run_name='closeout_frozen_source'))
    cache=args.strategy_root/'data/raw/history_provider_v1'
    manifest=read_json(cache/'manifest.json')
    financial=[]
    for entry in manifest['completed'].values():
        period=entry['params'].get('period','')
        if entry['api']=='fina_indicator_vip' and period.endswith('1231') and '20130101'<=period<='20231231':
            if Path(entry['file']).name!=entry['file']: raise ValueError('Unsafe cache path')
            raw=read_json(cache/entry['file'],entry['sha256'])
            financial.append(pd.DataFrame(raw['data'],columns=raw['columns']))
    earlier=args.root/'lake/statements/fina_indicator.parquet';protected[earlier]=sha(earlier)
    extra=ds.dataset(earlier).to_table(columns=['ts_code','ann_date','end_date','profit_dedt'],
        filter=ds.field('end_date').isin(['20111231','20121231'])).to_pandas()
    financial.append(extra)
    financial=pd.concat(financial,ignore_index=True)
    folder=args.strategy_root/'outputs/history_provider_v1'
    schedule_path=folder/'schedule.csv';protected[schedule_path]=sha(schedule_path)
    schedule=pd.read_csv(schedule_path,dtype=str)
    periods={'development':('20150101','20201231','2014-12-01','2020-12-31',71),
             'validation':('20210101','20231231','2020-12-01','2023-12-29',36)}
    all_targets={};coverage={};first_days={}
    for phase,(begin,end,warmup,last,count) in periods.items():
        steps=schedule[schedule.execution_date.between(begin,end)].sort_values('signal_date')
        if len(steps)!=count or steps.signal_date.duplicated().any(): raise ValueError('Incomplete frozen schedule')
        names=['NC30']+(['NC','V','R30'] if phase=='development' else [])+[f'MC_{s}' for s in range(20261016,20261021)]
        groups={n:[] for n in names};coverage[phase]={n:[] for n in names}
        first_days[phase]=date.fromisoformat(steps.iloc[0].execution_date[:4]+'-'+steps.iloc[0].execution_date[4:6]+'-'+steps.iloc[0].execution_date[6:]).isoformat()
        for step in steps.to_dict('records'):
            signal,execution=(date.fromisoformat(step[k][:4]+'-'+step[k][4:6]+'-'+step[k][6:]) for k in ('signal_date','execution_date'))
            if not signal<execution or execution.year>2023: raise ValueError('Invalid research date')
            path=folder/f'panel_{step["signal_date"]}.csv';protected[path]=sha(path)
            frame=pd.read_csv(path,dtype={'ts_code':str,'signal_date':str})
            if frame.ts_code.duplicated().any() or not frame.signal_date.eq(step['signal_date']).all(): raise ValueError('Invalid panel')
            normal=functions['normalized_deducted_yield'](financial,step['signal_date'],frame[['ts_code','annual_end_year','total_mv']])
            frame=frame.merge(normal,on='ts_code',how='left',validate='one_to_one')
            nc=treatment_frames(frame)['NC']
            for name in names:
                item=frame if name in ('V','R30') else nc
                variant='R30' if name=='R30' or name=='MC_20261016' else name.replace('MC_','R30_') if name.startswith('MC_') else 'V'
                ranked=functions['build_targets'](item,variant,30)
                weights=capped_weights(ranked) if name=='NC30' or name.startswith('MC_') else ranked[ranked.target_weight.gt(0)].set_index('ts_code').target_weight.to_dict()
                groups[name].append(TargetWeights(signal_date=signal,execution_date=execution,weights=weights,strategy_id=f'closeout-{phase}-{name}'))
                coverage[phase][name].append(len(weights))
        all_targets[phase]=groups
    old=read_json(args.cap/'NC30.json',next(r['artifact_sha256'] for r in cap['runs'] if r['name']=='NC30'))
    if [dict(t.weights) for t in all_targets['validation']['NC30']]!=[d['weights'] for d in old['decisions']]: raise ValueError('Frozen NC30 changed')
    for path,expected in protected.items():
        if sha(path)!=expected: raise ValueError('Input changed before replay')
    args.output.mkdir(parents=True,exist_ok=True)
    frozen={phase:{n:[t.model_dump(mode='json') for t in group] for n,group in groups.items()} for phase,groups in all_targets.items()}
    plan=repo/'docs/superpowers/plans/2026-11-17-value-closeout.md'
    provenance=dict(bundle_sha256=digest(frozen),plan_sha256=sha(plan),holdout_evaluated=False)
    (args.output/'frozen_targets.json').write_text(json.dumps(frozen,indent=2),encoding='utf-8')
    resolutions=read_json(repo/'examples/data/value_action_resolutions.json')
    facts=read_json(FACTS_PATH)
    report=dict(status='RUNNING',holdout_evaluated=False,plan_sha256=sha(plan),script_sha256=sha(__file__),
        target_bundle_sha256=provenance['bundle_sha256'],early_annual_rows=len(extra),phases={})
    errors=[]
    for phase,groups in all_targets.items():
        warmup,last=periods[phase][2:4]
        destination=args.output/phase;destination.mkdir()
        union=sorted({s for group in groups.values() for t in group for s in t.weights})
        resolution_file=destination/'action_resolutions.json'
        resolution_file.write_text(json.dumps(dict(resolutions=[r for r in resolutions['resolutions'] if r['symbol'] in union])),encoding='utf-8')
        snapshot=build_snapshot(args.root,destination/'derived_lake',union,warmup,last,resolution_file)
        snapshot,_=reconcile_history(destination/'derived_lake',facts,warmup,last)
        rows=[];report['phases'][phase]=dict(first_execution=first_days[phase],snapshot_sha256=snapshot['snapshot_sha256'],runs=rows)
        for name,targets in groups.items():
            try:
                symbols=sorted({s for t in targets for s in t.weights})
                data=load_tushare_lake(destination/'derived_lake',symbols,warmup,last)
                apply_declared_lifecycle_rules(data,facts)
                data.corporate_actions=[a.model_copy(update={'fractional_policy':'cash_in_lieu'}) if a.bonus_ratio+a.reserve_ratio>0 else a for a in data.corporate_actions]
                prepared=data.prepare()
                result,decisions=replay(data,prepared,targets,provenance)
                actual=result.to_dict()
                if phase=='validation' and name=='NC30':
                    if economic_path(actual)!=economic_path(old['result']): raise ValueError('Frozen validation NC30 financial path differs')
                    report['validation_NC30_economic_path_equal']=True
                artifact=destination/f'{name}.json'
                artifact.write_text(json.dumps(dict(result=actual,decisions=decisions,execution_outcomes=execution_outcomes(decisions,result)),default=str,ensure_ascii=False,indent=2),encoding='utf-8')
                annual={};previous=D(1000000)
                for year in sorted({r['date'][:4] for r in result.daily_nav if r['date']>=first_days[phase]}):
                    final=D([r for r in result.daily_nav if r['date'].startswith(year)][-1]['total_equity'])
                    annual[year]=float(final/previous-1);previous=final
                row=dict(name=name,status='COMPLETE',metrics=period_metrics(result.daily_nav,first_days[phase]),annual_returns=annual,
                    min_selected=min(coverage[phase][name]),fills=len(result.fills),fees=str(sum((D(f['fee']) for f in result.fills),D(0))),artifact_sha256=sha(artifact))
                del result,actual,prepared,data
            except Exception as error:
                errors.append(dict(phase=phase,name=name,type=type(error).__name__,reason=str(error)))
                row=dict(name=name,status='BLOCKED',error_type=type(error).__name__,reason_sha256=digest(str(error)))
            rows.append(row);print(phase,name,row.get('metrics',row['status']),flush=True)
            (args.output/'progress.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            gc.collect()
        by_name={r['name']:r for r in rows if r['status']=='COMPLETE'}
        report['phases'][phase]['gate']=period_gate(by_name.get('NC30',{}).get('metrics'),[by_name[f'MC_{s}']['metrics'] for s in range(20261016,20261021) if f'MC_{s}' in by_name])
    index_path=folder/'csi300_price_index.csv';protected[index_path]=sha(index_path)
    index=pd.read_csv(index_path,dtype={'trade_date':str})
    report['csi300_price_reference']={}
    for phase,(_a,_b,_w,last,_c) in periods.items():
        signal=all_targets[phase]['NC30'][0].signal_date.strftime('%Y%m%d')
        rows=index[index.trade_date.between(signal,last.replace('-',''))].sort_values('trade_date')
        report['csi300_price_reference'][phase]=dict(start=rows.iloc[0].trade_date,end=rows.iloc[-1].trade_date,total_return=float(rows.iloc[-1].close/rows.iloc[0].close-1))
    report['decision']='RETAIN_FOR_NEXT_INDEPENDENT_REVIEW' if all(r['gate']['pass'] for r in report['phases'].values()) else 'DO_NOT_ADVANCE'
    if errors: report['decision']='DATA_BLOCKED'
    report['status']='CLOSEOUT_COMPLETED'
    for path,expected in protected.items():
        if sha(path)!=expected: raise ValueError('Original inputs changed during closeout')
    private=args.output/'private_review.json'
    private.write_text(json.dumps(dict(errors=errors,input_sha256={str(p):h for p,h in protected.items()}),ensure_ascii=False,indent=2),encoding='utf-8')
    report['private_review_sha256']=sha(private)
    (args.output/'summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')


if __name__=='__main__':
    main()
