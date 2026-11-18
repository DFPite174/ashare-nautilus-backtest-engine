# cython: language_level=3
"""Version-pinned Cython bridge for native NT long Equity split state.

This uses the 1.231.0 Cython ABI. It is not a public upstream API. The caller must
refresh native cache/portfolio and persist/replay the corporate-action journal.
Original fills are preserved, and money/realized PnL are never rewritten here.
"""
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_EVEN
from libc.stdint cimport uint64_t
from nautilus_trader.model.position cimport Position
from nautilus_trader.model.objects cimport Quantity
from nautilus_trader.model.objects import Quantity as PyQuantity
from nautilus_trader.model.instruments import Equity


cdef Quantity _historical_quantity(value):
    value = value.normalize()
    # The pinned NT wheel supports at most nine fractional digits in Quantity.
    if value.as_tuple().exponent < -9:
        value = value.quantize(Decimal("0.000000001"), rounding=ROUND_HALF_EVEN).normalize()
    return PyQuantity.from_str(format(value, "f"))


def apply_split(Position p, ratio, uint64_t ts_event, instrument, fractional_policy='reject'):
    if not isinstance(instrument,Equity) or instrument.id!=p.instrument_id:
        raise ValueError('Matching native Equity instrument required')
    ratio=Decimal(str(ratio))
    if not ratio.is_finite() or not Decimal('.0001')<=ratio<=Decimal(10000):
        raise ValueError('Invalid split ratio')
    if p.is_inverse or p.signed_qty<=0 or p.size_precision!=0:
        raise ValueError('Only open long integer-share positions are supported')
    if ts_event<p.ts_last:raise ValueError('Split predates position state')
    if fractional_policy not in ('reject','cash_in_lieu'):raise ValueError('Invalid fractional entitlement policy')
    old=p.to_dict()
    quantities=[Decimal(str(x))*ratio for x in (p.quantity,p.peak_qty,p._buy_qty,p._sell_qty)]
    whole=quantities[0].to_integral_value(rounding=ROUND_FLOOR)
    fraction=quantities[0]-whole
    if fraction and fractional_policy=='reject':
        raise ValueError('Fractional native Equity quantity; no silent rounding')
    if whole<=0:raise ValueError('Split would remove the entire native position; explicit close settlement required')
    # Validate/allocate every replacement before changing the native object.
    cdef Quantity q=PyQuantity.from_str(str(whole))
    # Historic turnover may be fractional in new units even when the live holding is integer.
    cdef Quantity peak=_historical_quantity(quantities[1])
    cdef Quantity buys=_historical_quantity(quantities[2])
    cdef Quantity sells=_historical_quantity(quantities[3])
    cdef double r=float(ratio)
    p.quantity=q
    p.peak_qty=peak
    p._buy_qty=buys
    p._sell_qty=sells
    p.signed_qty=float(whole)
    p.avg_px_open=p.avg_px_open/r
    p.avg_px_close=p.avg_px_close/r
    p.ts_last=ts_event
    return dict(ratio=str(ratio),ts_event=ts_event,before=old,after=p.to_dict(),fill_count=len(p.events),fractional_quantity=str(fraction),fractional_policy=fractional_policy)
