from dataclasses import dataclass


@dataclass(slots=True)
class TweetEvent:
    tweet_id: str
    author: str
    text: str
    url: str
    created_at: str
    tweet_type: str = "post"
    in_reply_to_status_id: str = ""
    in_reply_to_user: str = ""
    conversation_id: str = ""

