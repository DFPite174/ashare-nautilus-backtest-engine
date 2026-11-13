"""Append-only versioned maps with constant-time, read-only snapshots."""
from bisect import bisect_right
from collections.abc import Mapping
from dataclasses import dataclass
import weakref


class SnapshotMap:
    """Store immutable values; retain old versions needed by saved snapshots.

    Captures take O(1); reads take O(log retained versions for the key).
    Callers own value immutability and must not mutate stored values.

    无存活快照时，每个更新过的键只保留当前值，内存为 O(键数)。
    存活旧快照需要保留后续版本；键再次更新时回收已不需要的版本。
    """

    def __init__(self):
        self._keys = []
        self._revisions = {}
        self._values = {}
        self._revision = 0
        self._snapshots = weakref.WeakValueDictionary()
        self._capture = 0

    def update(self, key, value):
        if key in self._values:
            if self._values[key][-1] == value:
                self._prune(key)
                return
        else:
            self._keys.append(key)
            self._revisions[key] = []
            self._values[key] = []
        self._revision += 1
        self._revisions[key].append(self._revision)
        self._values[key].append(value)
        self._prune(key)

    def _prune(self, key):
        if len(self._revisions[key])<2:return
        oldest = next(iter(self._snapshots.values()), None) if self._snapshots else None
        revision = oldest._revision if oldest is not None else self._revision
        # Keep the predecessor visible to the oldest live view and all later values.
        discard = bisect_right(self._revisions[key], revision) - 1
        if discard > 0:
            del self._revisions[key][:discard]
            del self._values[key][:discard]

    def snapshot(self) -> Mapping:
        view = _Snapshot(self, self._revision, len(self._keys))
        self._capture += 1
        self._snapshots[self._capture] = view
        return view


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False)
class _Snapshot(Mapping):
    _source: SnapshotMap
    _revision: int
    _length: int

    def __getitem__(self, key):
        revisions = self._source._revisions[key]
        index = bisect_right(revisions, self._revision) - 1
        if index < 0:
            raise KeyError(key)
        return self._source._values[key][index]

    def __iter__(self):
        return (self._source._keys[n] for n in range(self._length))

    def __len__(self):
        return self._length
