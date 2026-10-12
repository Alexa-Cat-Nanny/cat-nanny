"""Telling the family about a red flag outside the conversation.

Alexa+ add-ons cannot push notifications to the Alexa app, so there are two
channels (both used together):
1. Email via an SNS topic, right away (`SnsNotifier`). SNS email needs no phone-number
   registration; each family member confirms the subscription email once.
2. The alert is stored and repeated in every tool result until someone
   acknowledges it, so whoever talks to Alexa next hears it (see tools.py).
"""

from __future__ import annotations

import logging
from typing import Protocol

log = logging.getLogger("cat_nanny.notify")


class Notifier(Protocol):
    def notify_family(self, subject: str, message: str) -> None: ...


class LogNotifier:
    """Local development: write the notification to the log instead of sending it."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def notify_family(self, subject: str, message: str) -> None:
        self.sent.append((subject, message))
        log.warning("FAMILY NOTIFICATION: %s | %s", subject, message)


class SnsNotifier:
    def __init__(self, topic_arn: str, client: object | None = None) -> None:
        if client is None:
            import boto3

            client = boto3.client("sns")
        self._sns = client
        self._topic_arn = topic_arn

    def notify_family(self, subject: str, message: str) -> None:
        # SNS email subjects must be < 100 chars, ASCII, no newlines.
        safe_subject = subject.encode("ascii", "ignore").decode()[:99].replace("\n", " ")
        self._sns.publish(TopicArn=self._topic_arn, Subject=safe_subject, Message=message)  # type: ignore[attr-defined]


def make_notifier(kind: str, topic_arn: str = "") -> Notifier:
    if kind == "sns":
        if not topic_arn:
            raise ValueError("NOTIFIER=sns needs SNS_TOPIC_ARN")
        return SnsNotifier(topic_arn)
    if kind == "log":
        return LogNotifier()
    raise ValueError(f"Unknown NOTIFIER {kind!r} (use log or sns)")
