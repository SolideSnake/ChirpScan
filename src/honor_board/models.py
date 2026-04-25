from dataclasses import dataclass


@dataclass(slots=True)
class HonorRecord:
    tweet_id: str
    source_author: str
    delivery_platform: str
    delivered_at: str
    summary: str = ""

