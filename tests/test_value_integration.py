from copy import deepcopy
import pytest

from examples.value_data_snapshot import resolve_actions
from engine.reproducibility import digest
import pandas as pd
from types import SimpleNamespace
import json


def test_action_resolution_is_snapshot_bound_not_sum_or_max():
    rows = [dict(ts_code='X', ex_date='20220714', cash_div_tax=2.),
            dict(ts_code='X', ex_date='20220714', cash_div_tax=.4)]
    rule = dict(symbol='X', ex_date='20220714', expected_row_sha256=sorted(digest(r) for r in rows),
                canonical=rows[0], evidence_url='https://example.org/notice', reason='2 includes .4')
    chosen, audit = resolve_actions(rows, [rule])
    assert chosen == [rows[0]] and len(audit) == 1
    changed = deepcopy(rows); changed[1]['cash_div_tax'] = .5
    with pytest.raises(ValueError, match='fingerprint'):
        resolve_actions(changed, [rule])
    wrong = deepcopy(rule); wrong['canonical']['ex_date'] = '20220715'
    with pytest.raises(ValueError, match='identity'):
        resolve_actions(rows, [wrong])


def test_targets_call_original_selector_without_reading_holdout_panels(tmp_path):
    from examples.integrate_value_strategy import load_targets
    src = tmp_path/'src/value_quant'; src.mkdir(parents=True)
    (src/'portfolio.py').write_text("def build_targets(frame, variant, top_n):\n    assert top_n == 30\n    return frame.assign(target_weight=[1/30, 0])\n", encoding='utf-8')
    panels = tmp_path/'outputs/history_provider_v1'; panels.mkdir(parents=True)
    pd.DataFrame([dict(signal_date='20201231', execution_date='20210104'),
                  dict(signal_date='20231229', execution_date='20240102')]).to_csv(panels/'schedule.csv', index=False)
    pd.DataFrame([dict(ts_code=s, signal_date='20201231', eligible=True, value_yield=.1) for s in ['A','B']]).to_csv(panels/'panel_20201231.csv', index=False)
    targets, provenance = load_targets(tmp_path)
    assert len(targets['V']) == 1 and targets['V'][0].weights == {'A': 1/30}
    assert provenance['holdout_evaluated'] is False
    assert len(provenance['panels_sha256']) == 1
    broken = pd.read_csv(panels/'panel_20201231.csv', dtype=str)
    broken['signal_date'] = '20210101'; broken.to_csv(panels/'panel_20201231.csv', index=False)
    with pytest.raises(ValueError, match='panel'):
        load_targets(tmp_path)


def test_private_snapshot_does_not_modify_original_lake(tmp_path):
    from test_tushare_lake import lake
    from examples.value_data_snapshot import build_snapshot, sha
    from engine.tushare_lake import load_tushare_lake
    original, derived = tmp_path/'original', tmp_path/'derived'
    lake(original)
    resolutions = tmp_path/'resolutions.json'; resolutions.write_text('{"resolutions": []}', encoding='utf-8')
    before = {p: sha(p) for p in original.rglob('*.parquet')}
    report = build_snapshot(original, derived, ['600000.SH'], '2025-05-06', '2025-05-08', resolutions)
    assert before == {p: sha(p) for p in before}
    assert report['action_resolution_audit'] == []
    assert len(load_tushare_lake(derived, ['600000.SH'], '2025-05-06', '2025-05-08').bars) == 3
    with pytest.raises(ValueError, match='overlap'):
        build_snapshot(original, original/'unsafe', ['600000.SH'], '2025-05-06', '2025-05-08', resolutions)


@pytest.mark.parametrize('target', ['database', 'strategy'])
def test_cli_rejects_input_overlapping_output_before_any_write(tmp_path, monkeypatch, target):
    from examples.integrate_value_strategy import main
    database, strategy = tmp_path/'database', tmp_path/'strategy'
    database.mkdir(); strategy.mkdir()
    monkeypatch.setattr('sys.argv', ['integration', '--root', str(database), '--strategy-root', str(strategy),
                                    '--output', str(tmp_path/target)])
    with pytest.raises(ValueError, match='overlap'):
        main()
    assert list(database.iterdir()) == [] and list(strategy.iterdir()) == []


def test_metrics_include_initial_cost_and_exclude_generated_holdout_observation():
    from examples.integrate_value_strategy import metrics
    result = SimpleNamespace(daily_nav=[dict(date=d, total_equity=v) for d, v in
        [('2020-12-31', 1000000), ('2021-01-04', 990000), ('2021-01-05', 999900), ('2024-01-02', 999999999)]])
    report = metrics(result)
    assert report['sessions'] == 2 and report['end'] == '2021-01-05'
    assert report['total_return'] == pytest.approx(-.0001)
    assert report['max_drawdown'] == pytest.approx(-.01)


def test_fraction_failure_context_preserves_exact_entitlement():
    from examples.integrate_value_strategy import action_failure_context
    from decimal import Decimal as D
    def _start_day():
        a = SimpleNamespace(symbol='X', ex_date='2022-06-16', bonus_ratio=D(0), reserve_ratio=D('.48'), fractional_policy='reject')
        pos = SimpleNamespace(quantity='286'); ratio = D('1.48')
        raise ValueError('Fractional native Equity quantity')
    try:
        _start_day()
    except ValueError as error:
        assert action_failure_context(error)['mathematical_quantity'] == '423.28'


def test_share_oracle_checks_quantity_and_approved_cash_independently():
    from examples.integrate_value_strategy import check_share_actions, fractional_cash_expected
    from datetime import date, datetime
    from zoneinfo import ZoneInfo
    from decimal import Decimal as D
    previous, ex = date(2022,6,15), date(2022,6,16)
    action = SimpleNamespace(symbol='X', ex_date=ex, bonus_ratio=D(0), reserve_ratio=D('.48'), fractional_policy='cash_in_lieu')
    bar = SimpleNamespace(symbol='X', end=datetime(2022,6,15,15,tzinfo=ZoneInfo('Asia/Shanghai')), close=D(148))
    data = SimpleNamespace(corporate_actions=[action], trading_days=[previous,ex], bars=[bar])
    fill = dict(symbol='X', side='BUY', quantity='10', time=int(datetime(2022,6,16,9,30,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()*1e9))
    result = SimpleNamespace(daily_nav=[dict(date=str(previous),holdings={'X':'286'}),dict(date=str(ex),holdings={'X':'433'})],
        fills=[fill], account_journal=[dict(event_id='X|2022-06-16|fraction',amount='28.00')])
    assert fractional_cash_expected(D(286),D('1.48'),D(148)) == D(28)
    check_share_actions(data,result)
    result.daily_nav[-1]['holdings']['X']='434'
    with pytest.raises(AssertionError): check_share_actions(data,result)
    result.daily_nav[-1]['holdings']['X']='433'
    result.account_journal[0]['amount']='28.01'
    with pytest.raises(AssertionError): check_share_actions(data,result)


def test_public_summary_hides_union_membership_without_mutating_private_report():
    from examples.integrate_value_strategy import public_summary
    private = dict(derived_snapshot={'symbols':['X','Y']}, runs=[dict(manifest={'config':{'symbol_fee_policy':{'X':'fee','Y':'fee'}}})])
    published = public_summary(private)
    assert 'symbols' not in published['derived_snapshot']
    assert 'symbol_fee_policy' not in published['runs'][0]['manifest']['config']
    assert private['derived_snapshot']['symbols'] == ['X','Y']


def test_replay_archives_weights_as_structured_json_not_frozen_mapping_text():
    from test_regular_backtest import fixture
    from examples.integrate_value_strategy import replay
    from engine import prepare_backtest
    from engine.tushare_lake import LakeInputs
    from contracts import Instrument, Venue
    from contracts.models import TargetWeights
    bars, rules = fixture(days=3, symbol='600000.SH')
    days = [b.end.date() for b in bars]
    instruments = [Instrument(symbol='600000.SH',exchange=Venue.SSE)]
    data = LakeInputs(instruments,bars,rules,days,[],{'snapshot_id':'fixture'})
    prepared = prepare_backtest(instruments,bars,rules,trading_days=days)
    _, decisions = replay(data,prepared,[TargetWeights(signal_date=days[0],execution_date=days[1],weights={'600000.SH':1.})],{'bundle_sha256':'fixture'})
    assert json.loads(json.dumps(decisions,default=str))[0]['weights'] == {'600000.SH':1.}
