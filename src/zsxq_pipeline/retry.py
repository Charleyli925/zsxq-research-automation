"""Durable retry timing shared by document processing and notifications."""

from datetime import timedelta


def retry_delay(attempt_count: int) -> timedelta:
    """Cool down 5, 10, 20, 40, then at most 60 minutes per attempt.

    A crash or an old configuration failure may increase the lifetime counter;
    that can lengthen a cooldown but must never make a temporary outage final.
    The bounded exponent also handles large historical counters safely.
    """

    exponent = min(max(int(attempt_count) - 1, 0), 4)
    return timedelta(minutes=min(5 * (2 ** exponent), 60))
