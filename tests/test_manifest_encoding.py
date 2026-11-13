"""Fingerprint compatibility must survive removal of deepcopy/materialization."""
from dataclasses import asdict, dataclass, is_dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
from collections.abc import Mapping
from zoneinfo import ZoneInfo

from engine.reproducibility import canonical, digest


def legacy(value):
    if hasattr(value, 'model_dump'): return legacy(value.model_dump(mode='json'))
    if is_dataclass(value): return legacy(asdict(value))
    if isinstance(value, Mapping): return {str(k): legacy(v) for k, v in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple)): return [legacy(v) for v in value]
    if isinstance(value, Decimal): return format(value.normalize(), 'f')
    if isinstance(value, datetime): return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, date): return value.isoformat()
    return value


@dataclass(frozen=True)
class Record:
    amount: Decimal
    time: datetime
    metadata: dict


def test_hash_matches_legacy_canonical_json():
    value = {'records': [Record(Decimal('1.200'), datetime(2025, 1, 2, tzinfo=timezone.utc),
        {'日': date(2025, 1, 2), 'negative_zero': Decimal('-0.00')})],
        ('stock', date(2025, 1, 2)): {'z': (True, None, 1.25), 'a': '中文'}}
    expected = json.dumps(legacy(value), sort_keys=True, ensure_ascii=False,
                          separators=(',', ':'), allow_nan=False)
    assert canonical(value) == legacy(value)
    assert digest(value) == hashlib.sha256(expected.encode('utf-8')).hexdigest()


def test_dataclass_fields_are_read_without_deepcopy():
    class NoCopyDecimal(Decimal):
        def __deepcopy__(self, memo):
            raise AssertionError('fingerprinting must not deepcopy immutable values')
    value = Record(NoCopyDecimal('1.20'), datetime(2025, 1, 2, tzinfo=timezone.utc), {})
    assert canonical(value)['amount'] == '1.2'
    assert len(digest(value)) == 64


def assert_legacy_hash(value):
    encoded = json.dumps(legacy(value), sort_keys=True, ensure_ascii=False,
                         separators=(',', ':'), allow_nan=False).encode('utf-8')
    assert canonical(value) == legacy(value)
    assert digest(value) == hashlib.sha256(encoded).hexdigest()


def test_string_key_collisions_keep_last_source_value():
    from types import MappingProxyType
    for pairs in (((1, {'x': 1}), ('1', [2])), (('1', [2]), (1, {'x': 1}))):
        collision = MappingProxyType(dict(pairs))
        for value in (collision, {'nested': collision}, {'rows': [collision]}):
            assert_legacy_hash(value)


def test_dataclass_field_names_cached_but_values_remain_live(monkeypatch):
    import engine.reproducibility as module
    @dataclass
    class Mutable:
        amount: Decimal
    original = module.fields
    calls = []
    def counted(value):
        calls.append(value)
        return original(value)
    monkeypatch.setattr(module, 'fields', counted)
    value = Mutable(Decimal('1.20'))
    assert_legacy_hash(value)
    value.amount = Decimal('2.30')
    assert_legacy_hash(value)
    assert_legacy_hash(Mutable(Decimal('3.40')))
    assert len(calls) == 1
    @dataclass
    class Extended(Mutable):
        label: str = 'extended'
    assert_legacy_hash(Extended(Decimal('4.50')))
    assert len(calls) == 2


def test_datetime_cache_distinguishes_dst_folds():
    zone = ZoneInfo('America/New_York')
    early = datetime(2025, 11, 2, 1, 30, tzinfo=zone, fold=0)
    late = early.replace(fold=1)
    assert early == late
    assert canonical(early) == '2025-11-02T05:30:00+00:00'
    assert canonical(late) == '2025-11-02T06:30:00+00:00'
    assert_legacy_hash([early, late, early, late])
