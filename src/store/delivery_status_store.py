from __future__ import annotations

import json
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Deque, Dict, List, Optional


@dataclass(slots=True)
class DeliveryRecord:
    platform: str
    tweet_id: str
    status: str
    reason: str = ""
    external_id: str = ""
    attempts: int = 0
    url: str = ""
    payload_text: str = ""
    retryable: bool = True
    updated_at: str = ""

    @property
    def success(self) -> bool:
        return self.status in {"success", "dry_run"}

    @classmethod
    def create(
        cls,
        *,
        platform: str,
        tweet_id: str,
        status: str,
        reason: str = "",
        external_id: str = "",
        attempts: int = 0,
        url: str = "",
        payload_text: str = "",
        retryable: bool = True,
    ) -> "DeliveryRecord":
        return cls(
            platform=platform,
            tweet_id=tweet_id,
            status=status,
            reason=reason,
            external_id=external_id,
            attempts=attempts,
            url=url,
            payload_text=payload_text,
            retryable=retryable,
            updated_at=datetime.now(timezone.utc).isoformat(),
        )


class DeliveryStatusStore:
    def __init__(
        self,
        storage_file: str,
        max_records: int = 5000,
        legacy_storage_file: str = ".state/sync_status.json",
    ) -> None:
        self._storage = Path(storage_file)
        self._legacy_storage = Path(legacy_storage_file)
        self._max_records = max(1, max_records)
        self._order: Deque[str] = deque(maxlen=self._max_records)
        self._records: Dict[str, DeliveryRecord] = {}

    def _make_key(self, platform: str, tweet_id: str) -> str:
        return f"{platform}:{tweet_id}"

    def _source_path(self) -> Path | None:
        if self._storage.exists():
            return self._storage
        if self._legacy_storage.exists():
            return self._legacy_storage
        return None

    def load(self) -> None:
        source_path = self._source_path()
        if source_path is None:
            return

        try:
            raw = json.loads(source_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        items = raw.get("records", [])
        if not isinstance(items, list):
            return

        self._order.clear()
        self._records.clear()
        for item in items[-self._max_records :]:
            if not isinstance(item, dict):
                continue
            try:
                record = DeliveryRecord(
                    platform=str(item.get("platform", "")).strip(),
                    tweet_id=str(item.get("tweet_id", "")).strip(),
                    status=str(item.get("status", "")).strip(),
                    reason=str(item.get("reason", "")).strip(),
                    external_id=str(item.get("external_id", "")).strip(),
                    attempts=int(item.get("attempts", 0) or 0),
                    url=str(item.get("url", item.get("post_url", ""))).strip(),
                    payload_text=str(item.get("payload_text", "")).strip(),
                    retryable=bool(item.get("retryable", True)),
                    updated_at=str(item.get("updated_at", "")).strip(),
                )
            except (TypeError, ValueError):
                continue
            if not record.platform or not record.tweet_id:
                continue
            key = self._make_key(record.platform, record.tweet_id)
            self._order.append(key)
            self._records[key] = record

    def save(self) -> None:
        self._storage.parent.mkdir(parents=True, exist_ok=True)
        payload = {"records": [asdict(self._records[key]) for key in self._order if key in self._records]}
        tmp_file = self._storage.with_suffix(self._storage.suffix + ".tmp")
        tmp_file.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
        tmp_file.replace(self._storage)

    def get(self, platform: str, tweet_id: str) -> Optional[DeliveryRecord]:
        return self._records.get(self._make_key(platform, tweet_id))

    def status_for(self, platform: str, tweet_id: str) -> Optional[Dict[str, object]]:
        record = self.get(platform, tweet_id)
        if not record:
            return None
        return asdict(record)

    def save_record(self, record: DeliveryRecord) -> DeliveryRecord:
        if not record.updated_at:
            record.updated_at = datetime.now(timezone.utc).isoformat()
        key = self._make_key(record.platform, record.tweet_id)
        if key not in self._records and len(self._order) == self._max_records:
            oldest = self._order[0]
            self._records.pop(oldest, None)
        if key not in self._records:
            self._order.append(key)
        self._records[key] = record
        self.save()
        return record

    def contains(self, platform: str, tweet_id: str) -> bool:
        return self.get(platform, tweet_id) is not None

    def should_skip_success(self, platform: str, tweet_id: str) -> bool:
        record = self.get(platform, tweet_id)
        return bool(record and record.status == "success")

    def all_records(self) -> List[DeliveryRecord]:
        return [self._records[key] for key in self._order if key in self._records]
