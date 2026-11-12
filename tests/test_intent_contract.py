"""Pure intent contract checks, independent of NautilusTrader imports."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

_spec = importlib.util.spec_from_file_location("intent_contract_under_test", Path(__file__).parents[1] / "engine" / "intents.py")
_module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _module
_spec.loader.exec_module(_module)
OrderIntent, validate_intents = _module.OrderIntent, _module.validate_intents
NOW = datetime(2026, 1, 5, 7, tzinfo=timezone.utc)
MASTER = {"600000.SH": SimpleNamespace(tick_size=Decimal(".01"))}


def intent(**kwargs):
    return OrderIntent("600000.SH", "buy", 100, **kwargs)


def test_existing_positionals_and_optional_metadata():
    old = OrderIntent("600000.SH", "BUY", 100, Decimal("10"))
    assert old.intent_id is None and old.earliest_execution is None
    new = intent(intent_id="decision-1", earliest_execution=NOW)
    seen = set()
    assert validate_intents(iter([old, new]), MASTER, NOW, seen) == [old, new]
    assert seen == {"decision-1"}


@pytest.mark.parametrize("kwargs", [
    {"intent_id": ""}, {"intent_id": "   "}, {"intent_id": 123},
    {"earliest_execution": NOW.replace(tzinfo=None)},
    {"earliest_execution": NOW - timedelta(microseconds=1)},
    {"earliest_execution": "2026-01-05"},
    {"limit_price": Decimal("NaN")}, {"limit_price": Decimal("Infinity")},
    {"limit_price": Decimal("0")}, {"limit_price": Decimal("10.001")},
    {"limit_price": "invalid"},
])
def test_invalid_metadata_or_price_rejects_atomic_batch(kwargs):
    seen = {"previous"}
    with pytest.raises(ValueError):
        validate_intents([intent(intent_id="new"), intent(**kwargs)], MASTER, NOW, seen)
    assert seen == {"previous"}


@pytest.mark.parametrize("bad", [
    OrderIntent("unknown", "BUY", 100), OrderIntent("600000.SH", "HOLD", 100),
    OrderIntent("600000.SH", None, 100), OrderIntent("600000.SH", "BUY", True),
    OrderIntent("600000.SH", "BUY", 0), OrderIntent("600000.SH", "BUY", 1.0),
    object(),
])
def test_existing_validation(bad):
    with pytest.raises(ValueError):
        validate_intents([bad], MASTER, NOW, set())


def test_ids_unique_within_batch_and_across_decisions():
    seen = set()
    with pytest.raises(ValueError, match="Duplicate"):
        validate_intents([intent(intent_id="same"), intent(intent_id="same")], MASTER, NOW, seen)
    assert not seen
    validate_intents([intent(intent_id="same")], MASTER, NOW, seen)
    with pytest.raises(ValueError, match="Duplicate"):
        validate_intents([intent(intent_id="same")], MASTER, NOW, seen)


def test_later_execution_and_equivalent_timezone_accepted():
    zone = timezone(timedelta(hours=8))
    values = [intent(earliest_execution=NOW.astimezone(zone)), intent(earliest_execution=NOW + timedelta(days=1))]
    assert validate_intents(values, MASTER, NOW, set()) == values


def test_naive_decision_time_rejected():
    with pytest.raises(ValueError, match="decision_time"):
        validate_intents([], MASTER, NOW.replace(tzinfo=None), set())
