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
    in_reply_to_tweet_id: str = ""
    in_reply_to_user: str = ""
    in_reply_to_user_id: str = ""
    conversation_id: str = ""

    def __post_init__(self) -> None:
        if self.in_reply_to_tweet_id and not self.in_reply_to_status_id:
            self.in_reply_to_status_id = self.in_reply_to_tweet_id
        elif self.in_reply_to_status_id and not self.in_reply_to_tweet_id:
            self.in_reply_to_tweet_id = self.in_reply_to_status_id

        if (
            self.tweet_type == "post"
            and (
                self.in_reply_to_tweet_id
                or self.in_reply_to_status_id
                or (self.conversation_id and self.conversation_id != self.tweet_id)
            )
        ):
            self.tweet_type = "reply"

