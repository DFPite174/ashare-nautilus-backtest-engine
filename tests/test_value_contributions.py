from decimal import Decimal as D
import pytest

from examples.analyze_value_contributions import security_pnl, target_exposure


def test_security_pnl_includes_dividends_fees_and_final_inventory():
    result = dict(fills=[dict(symbol='A',side='BUY',quantity='100',price='10',fee='5'),
                        dict(symbol='A',side='SELL',quantity='40',price='11',fee='5')],
        account_journal=[dict(event_id='A|date|dividend',amount='20'),
                         dict(event_id='A|date|fraction',amount='1')],
        daily_nav=[dict(holdings={'A':'60'},positions_value='660',total_equity='2111',receivable_dividends='0')])
    assert security_pnl(result, {'A':D(11)}, D(2000)) == {'A':D(111)}
    with pytest.raises(ValueError,match='valuation'):
        security_pnl(result, {'A':D(12)}, D(2000))
    result['daily_nav'][0]['total_equity']='2111.01'
    with pytest.raises(ValueError,match='profits'):
        security_pnl(result, {'A':D(11)}, D(2000))
    result['daily_nav'][0]['receivable_dividends']='0.01'
    with pytest.raises(ValueError,match='receivables'):
        security_pnl(result, {'A':D(11)}, D(2000))


def test_target_exposure_uses_weights_and_keeps_missing_sector_explicit():
    panel={'A':dict(industry='sector',value_yield='0.1'),
           'B':dict(industry='',value_yield='0.2')}
    report=target_exposure({'A':0.75,'B':0.25},panel)
    assert report['industry_weights']=={'sector':0.75,'UNKNOWN':0.25}
    assert report['weighted_value_yield']==pytest.approx(0.125)
    with pytest.raises(ValueError,match='Missing target'):
        target_exposure({'C':1},panel)
