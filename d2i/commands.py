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
    """ASCII words match whole ("teacher" is not "teach"); non-ASCII (Tamil) words match as a prefix, so a suffixed
    form still works. Lookarounds, because \\b fails after Tamil vowel signs."""
    t = (text or "").lower()
    pat = lambda w: rf"(?<!\w){re.escape(w)}" + (r"(?!\w)" if w.isascii() else "")
    return next((cmd for cmd, words in _TABLE if any(re.search(pat(w), t) for w in words)), None)
