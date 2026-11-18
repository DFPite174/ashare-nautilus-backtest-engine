"""Known provider exceptions for account entitlement, backed by disclosures."""
from datetime import date
from decimal import Decimal
import json
from pathlib import Path

from engine.reproducibility import digest


FACTS_PATH = Path(__file__).with_name('company_action_facts.json')


def load_company_action_facts():
    return json.loads(FACTS_PATH.read_text(encoding='utf-8'))


def filter_entitlement_rows(rows, facts, symbols, first, last):
    conditional = sorted(
        (event for event in facts['conditional_zero_entitlement']
         if event['symbol'] in symbols and first <= event['record_date'] <= last),
        key=lambda event: (event['record_date'], event['symbol']),
    )
    exceptions = {
        (event['symbol'], event['record_date']): event
        for event in (*facts['conditional_zero_entitlement'], *facts.get('no_account_entitlement', ()))
        if event['symbol'] in symbols
    }
    excluded = []
    kept = []
    for row in rows:
        key = row['ts_code'], row.get('record_date')
        event = exceptions.get(key)
        if event is None or row.get('div_proc', '实施') != '实施':
            kept.append(row)
            continue
        relevant = any(first <= str(row.get(field) or '') <= last
                       for field in ('record_date', 'ex_date', 'pay_date', 'div_listdate'))
        if not relevant:
            kept.append(row)
            continue
        if digest(row) != event['row_sha256']:
            raise ValueError(f'Company action source changed for {key}; review disclosure: {event.get("evidence_url")}')
        excluded.append(event)
    return kept, conditional, excluded


def verify_zero_entitlements(nav, events):
    if not events:
        return
    by_date = {row['date']: row for row in nav}
    blocked = []
    for event in events:
        day = date.fromisoformat(event['record_date']).isoformat()
        symbol = event['symbol']
        if day not in by_date:
            raise ValueError(f'Missing entitlement record snapshot: {symbol} {day}')
        quantity = Decimal(by_date[day]['holdings'].get(symbol, 0))
        if quantity:
            blocked.append(f'{symbol} {day} quantity={quantity} ({event.get("kind", "unresolved")})')
    if blocked:
        raise ValueError('Unsupported entitlement: ' + ' | '.join(blocked))
