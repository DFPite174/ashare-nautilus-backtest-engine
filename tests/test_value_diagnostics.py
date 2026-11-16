from copy import deepcopy
import pytest

from examples.diagnose_value_strategy import pnl_bridge, residual_tag, target_diagnostics


def test_pnl_bridge_exactly_reconciles_money_without_claiming_counterfactual():
    result = dict(fills=[dict(side='BUY',quantity='100',price='10',fee='5'),
                         dict(side='SELL',quantity='40',price='11',fee='5')],
        account_journal=[dict(event_id='X|dividend',amount='20'),dict(event_id='X|fraction',amount='1')],
        daily_nav=[dict(total_equity='2111',positions_value='660',receivable_dividends='0')])
    bridge = pnl_bridge(result, '2000')
    assert bridge['net_pnl'] == '111.00'
    assert bridge['trade_and_ending_value'] == '100.00'
    assert bridge['paid_fees'] == '10.00'
    wrong = deepcopy(result); wrong['daily_nav'][0]['total_equity']='2111.01'
    with pytest.raises(ValueError, match='reconcile'):
        pnl_bridge(wrong, '2000')


def test_fully_filled_old_quantity_is_not_full_liquidation_after_overnight_split():
    outcome = dict(outcome='FILLED',requested_quantity=4100,filled_quantity='4100',rejection_reasons=[])
    assert residual_tag([],outcome,'4100',['1.2'],'820') == 'OVERNIGHT_SHARE_ACTION_AFTER_SIZING'
    assert residual_tag([],outcome,'4100',[],'820') == 'UNCLASSIFIED'
    assert residual_tag(['INACTIVE'],None,'4100',[],'4100') == 'PLANNING:INACTIVE'


def test_targets_take_effect_on_execution_date_not_signal_close():
    old = dict(date='2020-12-31', execution_date='2021-01-04', decision_time='first',
               weights={'OLD':1}, holdings={}, allocations=[])
    new = dict(date='2021-01-29', execution_date='2021-02-01', decision_time='second',
               weights={'NEW':1}, holdings={'OLD':'100'},
               allocations=[dict(symbol='OLD',reasons=['INACTIVE'])])
    artifact = dict(decisions=[old,new],execution_outcomes=[],result=dict(daily_nav=[
        dict(date='2021-01-29',holdings={'OLD':'100'},cash='100',total_equity='1000'),
        dict(date='2021-02-01',holdings={'OLD':'100','NEW':'100'},cash='100',total_equity='2000')]))
    report, details = target_diagnostics(artifact,[])
    assert report['counts']['days_with_non_target_holdings']==1
    assert report['counts']['days_above_target_count']==1
    assert report['non_target_holding_day_tags']=={'PLANNING:INACTIVE':1}
    assert details[0]['date']=='2021-02-01'
