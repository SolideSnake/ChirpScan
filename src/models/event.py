from dataclasses import dataclass


@dataclass(slots=True)
class TweetEvent:
    tweet_id: str
    author: str
    text: str
    url: str
    created_at: str

