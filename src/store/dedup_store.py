import json
from collections import deque
from pathlib import Path
from typing import Deque, Set


class DedupStore:
    def __init__(self, storage_file: str, max_ids: int = 5000) -> None:
        self._storage = Path(storage_file)
        self._max_ids = max_ids
        self._order: Deque[str] = deque(maxlen=max_ids)
        self._seen: Set[str] = set()

    def load(self) -> None:
        if not self._storage.exists():
            return

        raw = json.loads(self._storage.read_text(encoding="utf-8"))
        ids = raw.get("tweet_ids", [])
        for tweet_id in ids[-self._max_ids :]:
            self._order.append(tweet_id)
            self._seen.add(tweet_id)

    def save(self) -> None:
        self._storage.parent.mkdir(parents=True, exist_ok=True)
        payload = {"tweet_ids": list(self._order)}
        tmp_file = self._storage.with_suffix(self._storage.suffix + ".tmp")
        tmp_file.write_text(json.dumps(payload, ensure_ascii=True), encoding="utf-8")
        tmp_file.replace(self._storage)

    def contains(self, tweet_id: str) -> bool:
        return tweet_id in self._seen

    def add_if_new(self, tweet_id: str) -> bool:
        if tweet_id in self._seen:
            return False

        if len(self._order) == self._max_ids:
            oldest = self._order[0]
            self._seen.discard(oldest)

        self._order.append(tweet_id)
        self._seen.add(tweet_id)
        return True

