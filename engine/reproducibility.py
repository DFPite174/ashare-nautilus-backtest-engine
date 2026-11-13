"""Content-addressed inputs and immutable, constant-copy research history."""
from collections.abc import Mapping, Sequence
from dataclasses import fields, is_dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import importlib.metadata
import inspect
import json
import platform
import subprocess
from pathlib import Path


class HistorySnapshot(Sequence):
    """A captured prefix of an append-only list; never exposes later bars."""
    __slots__ = ('__bars', '__length')

    def __init__(self, bars):
        self.__bars, self.__length = bars, len(bars)

    def __len__(self):
        return self.__length

    def __getitem__(self, index):
        if isinstance(index, slice):
            return tuple(self.__bars[n] for n in range(*index.indices(self.__length)))
        if index < 0: index += self.__length
        if not 0 <= index < self.__length: raise IndexError(index)
        return self.__bars[index]


def canonical(value):
    if hasattr(value, 'model_dump'): return canonical(value.model_dump(mode='json'))
    if is_dataclass(value): return {f.name: canonical(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Mapping): return {str(k): canonical(v) for k, v in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (tuple, list)): return [canonical(v) for v in value]
    if isinstance(value, Decimal): return format(value.normalize(), 'f')
    if isinstance(value, datetime): return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, date): return value.isoformat()
    return value


def digest(value):
    encoder = json.JSONEncoder(sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    result = hashlib.sha256()
    def write(item, depth=0):
        # Stream collection rows; C JSON encoding handles each bounded row.
        if depth < 2 and isinstance(item, Mapping):
            normalized = {str(k): v for k, v in sorted(item.items(), key=lambda pair: str(pair[0]))}
            result.update(b'{')
            for n, (key, child) in enumerate(sorted(normalized.items())):
                if n: result.update(b',')
                result.update(encoder.encode(key).encode('utf-8'));result.update(b':')
                write(child, depth+1)
            result.update(b'}')
        elif depth < 2 and isinstance(item, (list, tuple)):
            result.update(b'[')
            for n, child in enumerate(item):
                if n: result.update(b',')
                write(child, depth+1)
            result.update(b']')
        else:
            result.update(encoder.encode(canonical(item)).encode('utf-8'))
    write(value)
    return result.hexdigest()


def build_manifest(instruments, bars, rules, actions, sessions, fee, signal, *, initial_cash,
                   participation_rate, slippage_bps, strategy_id, strategy_params, data_snapshot_id,
                   _input_sha256=None,_data_delivery='full',_order_tape_sha256=None):
    root = Path(__file__).resolve().parents[1]
    try:
        commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, stderr=subprocess.DEVNULL, text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        commit = None
    sources = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
               for folder in ('engine', 'contracts') for p in sorted((root / folder).glob('*.py'))}
    for p in sorted((root / 'engine').glob('*.pyd')):
        sources[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    try:
        signal_hash = hashlib.sha256(inspect.getsource(signal).encode()).hexdigest()
    except (TypeError, OSError): signal_hash = None
    input_hash = _input_sha256 or digest({'instruments': sorted(instruments, key=lambda i:i.symbol),
        'bars': sorted(bars, key=lambda b:(b.start,b.symbol)), 'rules': rules,
        'actions': sorted(actions, key=lambda a:(a.symbol,a.ex_date)), 'calendar':sessions})
    # Explicit configuration only: never serialize function globals, environment variables or credentials.
    from engine.fee_model import AShareFeeModel, ConvertibleBondFeeModel
    builtin_rates=(type(fee) in (AShareFeeModel,ConvertibleBondFeeModel)
                   and getattr(fee.rates_for,'__func__',None) is AShareFeeModel.rates_for)
    schedules={};symbol_policies={};asset_policies={}
    for symbol in sorted(i.symbol for i in instruments):
        group=bool(getattr(fee,'is_cb_model',False) or symbol in fee.cb_symbols) if builtin_rates else symbol
        if group in asset_policies:
            symbol_policies[symbol]=asset_policies[group];continue
        schedule={};previous=None
        for day in sessions:
            rates=fee.rates_for(symbol,str(day))
            if rates!=previous:schedule[str(day)]=rates;previous=rates
        policy=digest(schedule)
        schedules[policy]=schedule;symbol_policies[symbol]=policy
        asset_policies[group]=policy
    config = dict(initial_cash=str(initial_cash), participation_rate=str(participation_rate),
                  slippage_bps=str(slippage_bps), execution_policy='same-open-sell-before-buy-v1',
                  execution_model='bar-synthetic-l2-v1',
                  data_delivery=_data_delivery,
                  fee_schedules=schedules,symbol_fee_policy=symbol_policies)
    manifest = dict(schema_version=1, input_sha256=input_hash, data_snapshot_id=data_snapshot_id,
        strategy_id=strategy_id or getattr(signal,'__qualname__',type(signal).__name__),
        strategy_source_sha256=signal_hash, strategy_params=canonical(strategy_params or {}),
        code_commit=commit, source_sha256=digest(sources),
        environment={'python':platform.python_version(), 'platform':platform.platform(),
                     **{name:importlib.metadata.version(name) for name in ('nautilus_trader','pydantic','numpy','pandas','pyarrow')}},
        config=canonical(config))
    if _order_tape_sha256 is not None:
        manifest['order_tape_sha256']=_order_tape_sha256
        manifest['decision_contract']='daily-close-frozen-tape-v1'
    manifest['run_sha256'] = digest(manifest)
    return manifest
