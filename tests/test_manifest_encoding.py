"""Fingerprint compatibility must survive removal of deepcopy/materialization."""
from dataclasses import asdict, dataclass, is_dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json

from engine.reproducibility import canonical, digest


def legacy(value):
    if hasattr(value, 'model_dump'): return legacy(value.model_dump(mode='json'))
    if is_dataclass(value): return legacy(asdict(value))
    if isinstance(value, dict): return {str(k): legacy(v) for k, v in sorted(value.items(), key=lambda item: str(item[0]))}
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
