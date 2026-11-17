from datetime import datetime, date
from decimal import Decimal as D
from types import SimpleNamespace
import pandas as pd
import pytest
from examples.run_value_industry_cap import capped_weights, realized_exposure


def test_cap_skips_saturated_sector_then_backfills_in_score_order():
    ranked=pd.DataFrame(dict(ts_code=['A','B','C','D','E'],score=[5,4,3,2,1],
                             industry=['X','X','X','Y','Y']))
    assert capped_weights(ranked,top_n=4,max_per_sector=2)=={'A':.25,'B':.25,'D':.25,'E':.25}
    assert capped_weights(ranked.iloc[:3],top_n=4,max_per_sector=2)=={'A':.25,'B':.25}


def test_missing_sector_is_one_bucket_and_ties_use_security_code():
    ranked=pd.DataFrame(dict(ts_code=['C','B','A'],score=[1,1,1],industry=[None,'','']))
    assert capped_weights(ranked,top_n=3,max_per_sector=1)=={'A':1/3}


def test_realized_exposure_uses_account_equity_and_latest_known_prices():
    nav=[dict(date='2021-01-04',holdings={'A':'10','B':'10'},positions_value='300',total_equity='400'),
         dict(date='2021-01-05',holdings={'A':'10','B':'10'},positions_value='400',total_equity='500')]
    bars=[SimpleNamespace(symbol=s,end=datetime.fromisoformat(day),close=p)
          for s,day,p in [('A','2021-01-04',10),('B','2021-01-04',20),('A','2021-01-05',20)]]
    panels={'2020-12-31':{'A':{'industry':'X'},'B':{'industry':'Y'}}}
    report=realized_exposure(nav,bars,panels)
    assert report['peak_weight']==.5
    assert report['days_above_30pct']==2
    nav[1]['positions_value']='401'
    with pytest.raises(ValueError,match='valuation'):
        realized_exposure(nav,bars,panels)


def test_ex_date_valuation_adjusts_when_no_new_quote_arrives():
    nav=[dict(date='2021-01-04',holdings={'A':'10'},positions_value='100',total_equity='100'),
         dict(date='2021-01-05',holdings={'A':'20'},positions_value='90',total_equity='100')]
    bar=SimpleNamespace(symbol='A',end=datetime(2021,1,4),close=10)
    action=SimpleNamespace(symbol='A',ex_date=date(2021,1,5),cash_div=D(1),bonus_ratio=D(1),reserve_ratio=D(0))
    report=realized_exposure(nav,[bar],{'2020-12-31':{'A':{'industry':'X'}}},[action])
    assert report['daily'][-1]['weights']=={'X':.9}


@pytest.mark.parametrize('destination',['root','p1','existing'])
def test_cli_preserves_protected_and_existing_outputs(tmp_path,monkeypatch,destination):
    from examples.run_value_industry_cap import main
    paths={k:tmp_path/k for k in ('root','p1','strategy','existing')}
    for path in paths.values(): path.mkdir()
    output=paths[destination]
    marker=output/'marker.txt';marker.write_text('preserve',encoding='utf-8')
    monkeypatch.setattr('sys.argv',['cap','--root',str(paths['root']),'--strategy-root',str(paths['strategy']),
        '--p1',str(paths['p1']),'--output',str(output)])
    with pytest.raises(ValueError,match='overlap|fresh'):
        main()
    assert marker.read_text(encoding='utf-8')=='preserve'
