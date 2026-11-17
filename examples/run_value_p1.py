"""Fixed P1 normalization/cash factorial experiment on validation-only monthly targets."""
import argparse
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import date
from decimal import Decimal as D
import json
from pathlib import Path
import runpy
import sys

import numpy as np
import pandas as pd

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from contracts.models import TargetWeights
from engine import prepare_backtest, compare_complete_backtests
from engine.reproducibility import digest
from engine.tushare_lake import load_tushare_lake
from examples.integrate_value_strategy import replay, metrics
from examples.research_workflow import execution_outcomes
from examples.value_data_snapshot import sha, check_output_location, build_snapshot
from examples.analyze_value_contributions import target_exposure


def treatment_frames(frame):
    normal=pd.to_numeric(frame.normalized_value_yield,errors='raise')
    coverage=np.isfinite(normal) & normal.gt(0) & frame.value_yield.gt(0)
    cash=pd.to_numeric(frame.cash_quality,errors='raise').ge(1)
    result={}
    for name in ('V','C','V_COV','C_COV','N','NC'):
        item=frame.copy(deep=True)
        if name in ('V_COV','C_COV','N','NC'):
            item['eligible'] &= coverage
        if name in ('C','C_COV','NC'):
            item['eligible'] &= cash
        if name in ('N','NC'):
            item['value_yield']=normal
        result[name]=item
    return result


def economic_path(result):
    path=deepcopy({k:result[k] for k in ('daily_nav','fills','positions','native_cash','ledger_cash','account_journal','rejections')})
    path['account_journal']=[r for r in path['account_journal'] if D(r['amount'])!=0]
    # Expanded universes export additional zero positions; keep every nonzero quantity.
    path['positions']={s:q for s,q in path['positions'].items() if D(q)!=0}
    for row in path['daily_nav']:
        for key in ('cash','positions_value','total_equity','receivable_dividends','native_cash','native_equity'):
            if key in row:
                row[key]=D(row[key])
        for key in ('holdings','tradable_holdings'):
            if key in row:
                row[key]={s:q for s,q in row[key].items() if D(q)!=0}
    return path


def compare_metrics(treatment,control):
    return dict(return_difference=float(treatment['total_return']-control['total_return']),
        max_drawdown_difference=treatment['max_drawdown']-control['max_drawdown'],
        return_higher_and_drawdown_not_worse=bool(treatment['total_return']>control['total_return'] and
                                                treatment['max_drawdown']>=control['max_drawdown']))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for flag in ('root','strategy-root','baseline','output'):
        parser.add_argument('--'+flag,type=Path,required=True)
    args=parser.parse_args()
    args.output=check_output_location(args.output,args.root,args.strategy_root,args.baseline)
    if args.output.exists() and any(args.output.iterdir()):
        raise ValueError('Use a fresh output directory to preserve existing experiments')
    protected={}
    def read_json(path,expected=None):
        protected[path]=sha(path)
        if expected is not None and protected[path]!=expected:
            raise ValueError('Frozen input changed: '+path.name)
        return json.loads(path.read_text(encoding='utf-8'))
    base=read_json(args.baseline/'summary.json')
    if base['holdout_evaluated'] or not base['status'].startswith('PASS_EXECUTION_ADAPTATION'):
        raise ValueError('Require frozen successful validation baseline')
    old=read_json(args.baseline/'V.json',next(r['artifact_sha256'] for r in base['runs'] if r['name']=='V'))
    src=args.strategy_root/'src/value_quant'
    functions={}
    for filename in ('portfolio.py','history_factors.py'):
        protected[src/filename]=sha(src/filename)
        functions.update(runpy.run_path(str(src/filename),run_name='frozen_p1_source'))
    cache=args.strategy_root/'data/raw/history_provider_v1'
    manifest=read_json(cache/'manifest.json')
    parts=[]
    for entry in manifest['completed'].values():
        period=entry['params'].get('period','')
        if entry['api']!='fina_indicator_vip' or not period.endswith('1231') or not '20130101'<=period<='20231231':
            continue
        if Path(entry['file']).name!=entry['file']:
            raise ValueError('Unsafe financial cache path')
        data=read_json(cache/entry['file'],entry['sha256'])
        if len(data['data'])!=entry['rows']:
            raise ValueError('Financial row count differs')
        parts.append(pd.DataFrame(data['data'],columns=data['columns']))
    financials=pd.concat(parts,ignore_index=True)
    targets={name:[] for name in ('V','C','V_COV','C_COV','N','NC')}
    panels,coverage={},{name:[] for name in targets}
    for decision in old['decisions']:
        signal=decision['date'].replace('-','')
        if not '20201231'<=signal<='20231231' or not '2021-01-01'<=decision['execution_date']<='2023-12-31':
            raise ValueError('Unexpected decision outside validation')
        filename=f'panel_{signal}.csv'
        path=args.strategy_root/'outputs/history_provider_v1'/filename
        protected[path]=base['upstream']['panels_sha256'][filename]
        if sha(path)!=protected[path]:
            raise ValueError('Frozen panel changed')
        frame=pd.read_csv(path,dtype={'ts_code':str,'signal_date':str})
        if not frame.signal_date.eq(signal).all() or frame.ts_code.duplicated().any():
            raise ValueError('Invalid panel identity')
        values=functions['normalized_deducted_yield'](financials,signal,frame[['ts_code','annual_end_year','total_mv']])
        frame=frame.merge(values,on='ts_code',how='left',validate='one_to_one')
        panels[decision['date']]=frame.set_index('ts_code').to_dict('index')
        for name,item in treatment_frames(frame).items():
            chosen=functions['build_targets'](item,'V',30)
            weights=chosen.loc[chosen.target_weight.gt(0)].set_index('ts_code').target_weight.to_dict()
            coverage[name].append(dict(date=decision['date'],eligible=int((item.eligible & item.value_yield.gt(0)).sum()),selected=len(weights)))
            targets[name].append(TargetWeights(signal_date=date.fromisoformat(decision['date']),
                execution_date=date.fromisoformat(decision['execution_date']),weights=weights,strategy_id=f'value-{name}-next-open'))
            if name=='V' and weights!=decision['weights']:
                raise ValueError('Original V targets changed')
    if any(len(group)!=36 for group in targets.values()):
        raise ValueError('Require 36 targets in each case')
    for path,expected in protected.items():
        if sha(path)!=expected:
            raise ValueError('Input changed during target construction')
    args.output.mkdir(parents=True,exist_ok=True)
    plan=Path(__file__).resolve().parents[1]/'docs/superpowers/plans/2026-11-17-value-p1.md'
    bundle={name:[t.model_dump(mode='json') for t in group] for name,group in targets.items()}
    provenance=dict(bundle_sha256=digest(bundle),plan_sha256=sha(plan),
        source_sha256={str(p):h for p,h in protected.items()},holdout_evaluated=False)
    (args.output/'frozen_targets.json').write_text(json.dumps(bundle,indent=2),encoding='utf-8')
    (args.output/'target_provenance.json').write_text(json.dumps(provenance,indent=2),encoding='utf-8')
    print('All six target bundles frozen before market loading',flush=True)
    symbols=sorted(set(base['derived_snapshot']['symbols']) | {s for group in targets.values() for t in group for s in t.weights})
    snapshot=build_snapshot(args.root,args.output/'derived_lake',symbols,'2020-12-01','2023-12-29',
        Path(__file__).with_name('data')/'value_action_resolutions.json')
    data=load_tushare_lake(args.output/'derived_lake',symbols,'2020-12-01','2023-12-29')
    data.corporate_actions=[a.model_copy(update={'fractional_policy':'cash_in_lieu'}) if a.bonus_ratio+a.reserve_ratio>0 else a for a in data.corporate_actions]
    prepared=prepare_backtest(data.instruments,data.bars,data.rules,trading_days=data.trading_days,corporate_actions=data.corporate_actions)
    report=dict(plan_sha256=sha(plan),script_sha256=sha(__file__),holdout_evaluated=False,
        baseline_summary_sha256=protected[args.baseline/'summary.json'],target_bundle_sha256=provenance['bundle_sha256'],
        symbols=len(symbols),bars=len(data.bars),snapshot_sha256=snapshot['snapshot_sha256'],runs=[])
    reference=None
    for name in (*targets,'NC-repeat'):
        variant='NC' if name=='NC-repeat' else name
        result,decisions=replay(data,prepared,targets[variant],base['upstream'] if variant=='V' else provenance)
        actual=result.to_dict()
        artifact=args.output/f'{name}.json'
        if name!='NC-repeat':
            artifact.write_text(json.dumps(dict(result=actual,decisions=decisions,execution_outcomes=execution_outcomes(decisions,result)),default=str,ensure_ascii=False,indent=2),encoding='utf-8')
        if name=='V':
            report['original_V_economic_path_equal']=economic_path(actual)==economic_path(old['result'])
            if not report['original_V_economic_path_equal']:
                for key in economic_path(actual):
                    if economic_path(actual)[key]!=economic_path(old['result'])[key]:
                        print('Baseline difference: '+key,flush=True)
                raise ValueError('Expanded-universe V financial path differs')
        if name=='NC':
            reference=result
        if name=='NC-repeat':
            repeat=compare_complete_backtests(reference,result)
            if not repeat['equal']:
                raise ValueError('NC repeat differs')
            report['NC_repeat_equal']=True
            continue
        mean_industry=defaultdict(float)
        peak_industry=0.
        for d in decisions:
            weights=target_exposure(d['weights'],panels[d['date']])['industry_weights']
            peak_industry=max(peak_industry,max(weights.values(),default=0))
            for sector,w in weights.items():
                mean_industry[sector]+=w/len(decisions)
        annual,previous={},D(1000000)
        for year in ('2021','2022','2023'):
            final=D([r for r in result.daily_nav if r['date'].startswith(year)][-1]['total_equity'])
            annual[year]=float(final/previous-1)
            previous=final
        row=dict(name=name,metrics=metrics(result),annual_returns=annual,fees=str(sum((D(f['fee']) for f in result.fills),D(0))),
            fills=len(result.fills),rejection_reasons=dict(Counter(r['reason'] for r in result.rejections)),
            mean_target_industry_weights=dict(sorted(mean_industry.items())),peak_target_industry_weight=peak_industry,
            industry_cap_experiment_triggered=peak_industry>=.3-1e-12,
            min_eligible=min(r['eligible'] for r in coverage[name]),min_selected=min(r['selected'] for r in coverage[name]),
            artifact_sha256=sha(artifact),manifest_sha256=digest(result.manifest))
        report['runs'].append(row)
        print(name,row['metrics'],flush=True)
    by_name={r['name']:r for r in report['runs']}
    report['comparisons']={}
    for treatment,control in [('C','V'),('N','V_COV'),('NC','N'),('NC','C_COV'),('V_COV','V'),('C_COV','C')]:
        a,b=(by_name[n]['metrics'] for n in (treatment,control))
        report['comparisons'][treatment+'_vs_'+control]=compare_metrics(a,b)
    for path,expected in protected.items():
        if sha(path)!=expected:
            raise ValueError('Original input changed during experiment')
    report['status']='P1_FIXED_EXPERIMENT_COMPLETED'
    (args.output/'summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')


if __name__=='__main__':
    main()
