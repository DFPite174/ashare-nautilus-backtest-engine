from statistics import median
import pytest
from examples.close_value_research import period_metrics, period_gate


def test_period_metrics_excludes_warmup_and_uses_actual_account():
    nav=[dict(date='2015-01-30',total_equity='1000000'),
         dict(date='2015-02-02',total_equity='1100000'),
         dict(date='2015-02-03',total_equity='990000')]
    report=period_metrics(nav,'2015-02-02')
    assert report['sessions']==2
    assert report['total_return']==pytest.approx(-.01)
    assert report['max_drawdown']==pytest.approx(-.1)


def test_gate_requires_five_controls_and_both_return_and_risk():
    controls=[dict(total_return=r,max_drawdown=-.2) for r in [.1,.2,.3,.4,.5]]
    assert period_gate(dict(total_return=.31,max_drawdown=-.19),controls)['pass']
    assert not period_gate(dict(total_return=.5,max_drawdown=-.21),controls)['pass']
    assert not period_gate(dict(total_return=.31,max_drawdown=-.19),controls[:4])['pass']
