"""Fixed NC versus NC30 experiment; monthly target cap, unchanged account execution."""
import argparse
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal as D
import json
from pathlib import Path
import runpy
import sys

import pandas as pd

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from contracts.models import TargetWeights
from engine import prepare_backtest, compare_complete_backtests
from engine.reproducibility import digest
from engine.tushare_lake import load_tushare_lake
from examples.run_value_p1 import treatment_frames, economic_path, compare_metrics
from examples.integrate_value_strategy import replay, metrics
from examples.research_workflow import execution_outcomes
from examples.analyze_value_contributions import target_exposure
from examples.value_data_snapshot import sha, check_output_location, build_snapshot


def sector(value):
    return str(value).strip() if pd.notna(value) and str(value).strip() else 'UNKNOWN'


def capped_weights(ranked,top_n=30,max_per_sector=9):
    chosen={};counts=Counter()
    for row in ranked[ranked.score.notna()].sort_values(['score','ts_code'],ascending=[False,True]).itertuples():
        label=sector(row.industry)
        if counts[label]>=max_per_sector:
            continue
        chosen[row.ts_code]=1/top_n;counts[label]+=1
        if len(chosen)==top_n:
            break
    return chosen


def realized_exposure(nav,bars,panels,actions=()):
    by_day=defaultdict(list);ex_dates=defaultdict(list)
    for bar in bars: by_day[str(bar.end.date())].append(bar)
    for action in actions: ex_dates[str(action.ex_date)].append(action)
    labels={};prices={};daily=[];peak=0.;peak_day=None;peak_sector=None;mean=defaultdict(float)
    panel_days=sorted(panels);panel_index=0
    for row in nav:
        day=row['date']
        while panel_index<len(panel_days) and panel_days[panel_index]<=day:
            labels.update({s:sector(r['industry']) for s,r in panels[panel_days[panel_index]].items()})
            panel_index+=1
        for a in ex_dates[day]:
            if a.symbol in prices:
                prices[a.symbol]=(prices[a.symbol]-a.cash_div)/(D(1)+a.bonus_ratio+a.reserve_ratio)
        for bar in by_day[day]: prices[bar.symbol]=D(str(bar.close))
        values=defaultdict(D)
        for symbol,quantity in row['holdings'].items():
            if D(quantity): values[labels.get(symbol,'UNKNOWN')]+=D(quantity)*prices[symbol]
        if abs(sum(values.values(),D(0))-D(row['positions_value']))>D('.000001'):
            raise ValueError('Daily sector valuation does not reconcile')
        if not '2021-01-01'<=day<='2023-12-31':
            continue
        weights={s:float(v/D(row['total_equity'])) for s,v in values.items()}
        for label,weight in weights.items():
            mean[label]+=weight
            if weight>peak: peak,peak_day,peak_sector=weight,day,label
        daily.append(dict(date=day,weights=weights))
    return dict(peak_weight=peak,peak_day=peak_day,peak_sector=peak_sector,
        days_above_30pct=sum(max(r['weights'].values(),default=0)>.3+1e-12 for r in daily),
        mean_weights={s:w/len(daily) for s,w in sorted(mean.items())},daily=daily)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for flag in ('root','strategy-root','p1','output'):
        parser.add_argument('--'+flag,type=Path,required=True)
    args=parser.parse_args()
    args.output=check_output_location(args.output,args.root,args.strategy_root,args.p1)
    if args.output.exists() and any(args.output.iterdir()):
        raise ValueError('Use a fresh output directory')
    protected={}
    def read_json(path,expected=None):
        protected[path]=sha(path)
        if expected is not None and protected[path]!=expected:
            raise ValueError('Frozen source changed: '+path.name)
        return json.loads(path.read_text(encoding='utf-8'))
    source=read_json(args.p1/'summary.json')
    if source['status']!='P1_FIXED_EXPERIMENT_COMPLETED' or source['holdout_evaluated']:
        raise ValueError('Require completed validation-only P1')
    prior=read_json(args.p1/'target_provenance.json')
    bundle=read_json(args.p1/'frozen_targets.json')
    if digest(bundle)!=source['target_bundle_sha256'] or prior['bundle_sha256']!=source['target_bundle_sha256']:
        raise ValueError('P1 target provenance differs')
    old=read_json(args.p1/'NC.json',next(r['artifact_sha256'] for r in source['runs'] if r['name']=='NC'))
    old_snapshot=read_json(args.p1/'derived_lake/snapshot_manifest.json')
    if old_snapshot['snapshot_sha256']!=source['snapshot_sha256'] or digest({k:v for k,v in old_snapshot.items() if k!='snapshot_sha256'})!=source['snapshot_sha256']:
        raise ValueError('P1 market snapshot differs')
    repo=Path(__file__).resolve().parents[1]
    for filename,expected in prior['source_sha256'].items():
        path=Path(filename)
        if not path.is_absolute(): path=repo/path
        check_output_location(args.output,path)
        protected[path]=expected
        if sha(path)!=expected: raise ValueError('P1 input changed')
    src=args.strategy_root/'src/value_quant'
    functions={}
    for filename in ('portfolio.py','history_factors.py'):
        path=src/filename
        if str(path) not in prior['source_sha256']:
            raise ValueError('Strategy root differs from P1')
        functions.update(runpy.run_path(str(path),run_name='frozen_cap_source'))
    cache=args.strategy_root/'data/raw/history_provider_v1'
    manifest=read_json(cache/'manifest.json',prior['source_sha256'][str(cache/'manifest.json')])
    parts=[]
    for entry in manifest['completed'].values():
        period=entry['params'].get('period','')
        if entry['api']=='fina_indicator_vip' and period.endswith('1231') and '20130101'<=period<='20231231':
            if Path(entry['file']).name!=entry['file']: raise ValueError('Unsafe cache path')
            raw=read_json(cache/entry['file'],entry['sha256'])
            parts.append(pd.DataFrame(raw['data'],columns=raw['columns']))
    financials=pd.concat(parts,ignore_index=True)
    targets={'NC':[],'NC30':[]};panels={};changes=[]
    for decision in old['decisions']:
        signal=decision['date'].replace('-','')
        if not '20201231'<=signal<='20231231' or not '2021-01-01'<=decision['execution_date']<='2023-12-31':
            raise ValueError('Unexpected validation date')
        path=args.strategy_root/'outputs/history_provider_v1'/f'panel_{signal}.csv'
        frame=pd.read_csv(path,dtype={'ts_code':str,'signal_date':str})
        normal=functions['normalized_deducted_yield'](financials,signal,frame[['ts_code','annual_end_year','total_mv']])
        frame=frame.merge(normal,on='ts_code',how='left',validate='one_to_one')
        panels[decision['date']]=frame.set_index('ts_code').to_dict('index')
        ranked=functions['build_targets'](treatment_frames(frame)['NC'],'V',30)
        original=ranked[ranked.target_weight.gt(0)].set_index('ts_code').target_weight.to_dict()
        if original!=decision['weights']: raise ValueError('NC targets differ from P1')
        capped=capped_weights(ranked)
        changes.append(dict(date=decision['date'],removed=sorted(set(original)-set(capped)),
                            added=sorted(set(capped)-set(original)),selected=len(capped)))
        for name,weights in [('NC',original),('NC30',capped)]:
            targets[name].append(TargetWeights(signal_date=date.fromisoformat(decision['date']),
                execution_date=date.fromisoformat(decision['execution_date']),weights=weights,strategy_id=f'value-{name}-next-open'))
    if any(len(group)!=36 for group in targets.values()): raise ValueError('Require 36 decisions')
    for path,expected in protected.items():
        if sha(path)!=expected: raise ValueError('Source changed during target construction')
    args.output.mkdir(parents=True,exist_ok=True)
    frozen={n:[t.model_dump(mode='json') for t in group] for n,group in targets.items()}
    plan=repo/'docs/superpowers/plans/2026-11-17-value-industry-cap.md'
    provenance=dict(bundle_sha256=digest(frozen),plan_sha256=sha(plan),holdout_evaluated=False)
    (args.output/'frozen_targets.json').write_text(json.dumps(frozen,indent=2),encoding='utf-8')
    # Retain the original P1 universe so only the strategy rule changes.
    symbols=sorted(set(old_snapshot['symbols']) | {s for group in targets.values() for t in group for s in t.weights})
    snapshot=build_snapshot(args.root,args.output/'derived_lake',symbols,'2020-12-01','2023-12-29',repo/'examples/data/value_action_resolutions.json')
    data=load_tushare_lake(args.output/'derived_lake',symbols,'2020-12-01','2023-12-29')
    data.corporate_actions=[a.model_copy(update={'fractional_policy':'cash_in_lieu'}) if a.bonus_ratio+a.reserve_ratio>0 else a for a in data.corporate_actions]
    prepared=prepare_backtest(data.instruments,data.bars,data.rules,trading_days=data.trading_days,corporate_actions=data.corporate_actions)
    report=dict(status='INDUSTRY_CAP_EXPERIMENT_COMPLETED',holdout_evaluated=False,plan_sha256=sha(plan),script_sha256=sha(__file__),
        p1_summary_sha256=protected[args.p1/'summary.json'],target_bundle_sha256=provenance['bundle_sha256'],snapshot_sha256=snapshot['snapshot_sha256'],
        binding_months=sum(bool(r['removed']) for r in changes),replaced_stock_months=sum(len(r['removed']) for r in changes),
        min_selected=min(r['selected'] for r in changes),runs=[])
    details={'target_changes':changes};reference=None
    for name in ('NC','NC30','NC30-repeat'):
        variant='NC30' if name=='NC30-repeat' else name
        result,decisions=replay(data,prepared,targets[variant],prior if variant=='NC' else provenance)
        if name=='NC30': reference=result
        if name=='NC30-repeat':
            if not compare_complete_backtests(reference,result)['equal']: raise ValueError('NC30 repeat differs')
            report['NC30_repeat_equal']=True
            continue
        actual=result.to_dict()
        artifact=args.output/f'{name}.json'
        artifact.write_text(json.dumps(dict(result=actual,decisions=decisions,execution_outcomes=execution_outcomes(decisions,result)),default=str,ensure_ascii=False,indent=2),encoding='utf-8')
        if name=='NC':
            report['original_NC_economic_path_equal']=economic_path(actual)==economic_path(old['result'])
            if not report['original_NC_economic_path_equal']: raise ValueError('Original NC financial path differs')
        peak=max(max(target_exposure(d['weights'],panels[d['date']])['industry_weights'].values(),default=0) for d in decisions)
        if name=='NC30' and peak>.3+1e-12: raise ValueError('Target industry cap exceeded')
        exposure=realized_exposure(result.daily_nav,data.bars,panels,data.corporate_actions)
        details[name]={'daily_industry_weights':exposure.pop('daily')}
        annual,previous={},D(1000000)
        for year in ('2021','2022','2023'):
            final=D([r for r in result.daily_nav if r['date'].startswith(year)][-1]['total_equity'])
            annual[year]=float(final/previous-1);previous=final
        report['runs'].append(dict(name=name,metrics=metrics(result),annual_returns=annual,
            fees=str(sum((D(f['fee']) for f in result.fills),D(0))),fills=len(result.fills),
            rejection_reasons=dict(Counter(r['reason'] for r in result.rejections)),target_peak_industry_weight=peak,
            realized_industry_exposure=exposure,artifact_sha256=sha(artifact)))
        print(name,report['runs'][-1]['metrics'],flush=True)
    report['comparison']=compare_metrics(report['runs'][1]['metrics'],report['runs'][0]['metrics'])
    for path,expected in protected.items():
        if sha(path)!=expected: raise ValueError('Original source changed during replay')
    private=args.output/'private_details.json'
    private.write_text(json.dumps(dict(**details,input_sha256={str(p):h for p,h in protected.items()}),ensure_ascii=False,indent=2),encoding='utf-8')
    report['private_details_sha256']=sha(private)
    (args.output/'summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')


if __name__=='__main__':
    main()
