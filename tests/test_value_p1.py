import pandas as pd
import pytest
import json
import numpy as np
from copy import deepcopy
from examples.run_value_p1 import treatment_frames, economic_path, compare_metrics


def test_treatments_keep_coverage_cash_and_ranking_separate():
    frame=pd.DataFrame(dict(ts_code=['A','B','C','D','E'],eligible=[True,True,True,True,False],
        value_yield=[.5,.4,.3,-.1,.9],normalized_value_yield=[.1,.6,float('nan'),.4,.8],
        cash_quality=[.9,1.,1.2,2.,2.]))
    before=frame.copy(deep=True)
    cases=treatment_frames(frame)
    assert set(cases)=={'V','C','V_COV','C_COV','N','NC'}
    assert cases['V'].eligible.tolist()==[True,True,True,True,False]
    assert cases['C'].eligible.tolist()==[False,True,True,True,False]
    assert cases['V_COV'].eligible.tolist()==[True,True,False,False,False]
    assert cases['C_COV'].eligible.tolist()==cases['NC'].eligible.tolist()==[False,True,False,False,False]
    assert cases['N'].value_yield.iloc[1]==.6
    assert cases['V_COV'].value_yield.iloc[1]==.4
    pd.testing.assert_frame_equal(frame,before)


def test_economic_path_keeps_fees_timing_and_daily_holdings():
    result=dict(daily_nav=[dict(cash='1',holdings={'A':'2'})],fills=[dict(symbol='A',fee='1',time=42)],
        positions={'A':'2'},native_cash='1',ledger_cash='1',account_journal=[],rejections=[])
    path=economic_path(result)
    result['fills'][0]['fee']='2'
    assert economic_path(result)!=path


def test_expanded_market_zero_inventory_does_not_change_economic_path():
    result=dict(daily_nav=[dict(cash='1',holdings={'A':'2'},tradable_holdings={'A':'2'})],fills=[],
        positions={'A':'2'},native_cash='1',ledger_cash='1',account_journal=[],rejections=[])
    expanded=deepcopy(result)
    expanded['positions']['UNTRADED']='0.0'
    expanded['daily_nav'][0]['holdings']['UNTRADED']='0'
    expanded['daily_nav'][0]['tradable_holdings']['UNTRADED']='0'
    assert economic_path(result)==economic_path(expanded)
    expanded['daily_nav'][0]['holdings']['UNTRADED']='1'
    assert economic_path(result)!=economic_path(expanded)


def test_cash_scale_is_not_a_financial_difference_but_one_cent_is():
    result=dict(daily_nav=[dict(cash='100',total_equity='100.0',holdings={})],fills=[],
        positions={},native_cash='100',ledger_cash='100',account_journal=[],rejections=[])
    other=deepcopy(result)
    other['daily_nav'][0].update(cash='100.00',total_equity='100.00')
    assert economic_path(result)==economic_path(other)
    other['daily_nav'][0]['cash']='100.01'
    assert economic_path(result)!=economic_path(other)


def test_comparison_keeps_a_losing_treatment_as_json_false():
    a=dict(total_return=np.float64(.1),max_drawdown=-.2)
    b=dict(total_return=np.float64(.2),max_drawdown=-.1)
    value=compare_metrics(a,b)
    assert json.loads(json.dumps(value))['return_higher_and_drawdown_not_worse'] is False


def test_cli_preserves_an_existing_experiment(tmp_path,monkeypatch):
    from examples.run_value_p1 import main
    output=tmp_path/'output';output.mkdir()
    report=output/'summary.json';report.write_text('original',encoding='utf-8')
    monkeypatch.setattr('sys.argv',['p1','--root',str(tmp_path/'db'),'--strategy-root',str(tmp_path/'strategy'),
        '--baseline',str(tmp_path/'baseline'),'--output',str(output)])
    with pytest.raises(ValueError,match='fresh'):
        main()
    assert report.read_text(encoding='utf-8')=='original'
