"""Spoken commands for gloved hands: a transcript in, one bench action name out. The action itself is run by the screen."""
import re

# First match wins, in this order.
_TABLE = [
    ("self_test", ("self test", "self-test", "selftest", "சுய சோதனை")),
    ("teach", ("teach",)),
    ("inspect", ("inspect", "சோதி")),
    ("hold", ("hold", "stop the line", "நிறுத்து")),
    ("record", ("record", "report", "அறிக்கை")),
]

KEYWORDS = ["blister", "pocket", "strip", "self test", "batch"]


def match(text: str) -> str | None:
    """Whole words only ("teacher" is not "teach"); lookarounds, because \\b fails after Tamil vowel signs."""
    t = (text or "").lower()
    return next((cmd for cmd, words in _TABLE
                 if any(re.search(rf"(?<!\w){re.escape(w)}(?!\w)", t) for w in words)), None)
