from datetime import date, datetime, timezone
from decimal import Decimal

import engine.prepared as module


def test_exact_immutable_scalars_skip_container_dispatch(monkeypatch):
    class UnexpectedCheck(type):
        def __instancecheck__(cls, value):
            raise AssertionError('Immutable scalar entered mapping dispatch')
    class UnexpectedMapping(metaclass=UnexpectedCheck):
        pass
    monkeypatch.setattr(module, 'Mapping', UnexpectedMapping)
    for value in ('600000.SH', 2 ** 100, -0.0, True, None, Decimal('1.200'),
                  date(2025, 5, 6), datetime(2025, 5, 6, tzinfo=timezone.utc)):
        assert module._freeze(value) is value
