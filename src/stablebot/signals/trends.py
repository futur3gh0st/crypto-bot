from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from stablebot.signals.x_client import XPost

_PEG = re.compile(
    r"\b(depeg|de-peg|depegged|lost the peg|broken peg|unbacked|bank ?run|"
    r"attestation fail|insolvent|off[- ]peg)\b",
    re.I,
)
_REG = re.compile(
    r"\b(sec|lawsuit|sanction|regulat|mica|freeze|ban(ned)?|doj|enforcement)\b",
    re.I,
)
_MINT = re.compile(
    r"\b(mint(ing)?|attestation passed|reserves? (full|ok|healthy)|all clear|"
    r"inflows?|peg (holds|held|stable)|fully backed)\b",
    re.I,
)
_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9]{2,}")
_STOP = {
    "the", "and", "for", "that", "this", "with", "from", "are", "was", "have",
    "has", "not", "but", "you", "your", "our", "they", "their", "about", "just",
    "http", "https", "www", "com", "amp",
}


def engagement_score(post: XPost) -> float:
    return (
        math.log1p(post.likes)
        + math.log1p(post.retweets)
        + 0.5 * math.log1p(post.replies)
        + 0.25 * math.log1p(post.followers)
    )


def classify_text(text: str) -> str:
    if _PEG.search(text):
        return "peg-stress"
    if _REG.search(text):
        return "regulatory"
    if _MINT.search(text):
        return "bullish-mint"
    return "noise"


def annotate(posts: list[XPost]) -> list[XPost]:
    for p in posts:
        p.label = classify_text(p.text)
        p.score = engagement_score(p)
    return posts


@dataclass
class TrendRollup:
    window: str  # hourly | daily
    period_start: datetime
    period_end: datetime
    n_posts: int
    counts: dict[str, int]
    fear_score: float
    top_terms: list[tuple[str, int]] = field(default_factory=list)
    note: str = ""

    def size_hint(self) -> str:
        if self.fear_score >= 0.85:
            return "skip"
        if self.fear_score >= 0.60:
            return "cut"
        return "normal"


def _bucket_start(ts: datetime, window: str) -> datetime:
    ts = ts.astimezone(timezone.utc)
    if window == "daily":
        return ts.replace(hour=0, minute=0, second=0, microsecond=0)
    return ts.replace(minute=0, second=0, microsecond=0)


def _fear(counts: dict[str, int], posts: list[XPost]) -> float:
    if not posts:
        return 0.0
    weight = {"peg-stress": 1.0, "regulatory": 0.7, "bullish-mint": -0.4, "noise": 0.0}
    num = 0.0
    den = 0.0
    for p in posts:
        w = max(p.score, 0.1)
        num += weight.get(p.label, 0.0) * w
        den += w
    if den <= 0:
        return 0.0
    # Map roughly [-0.4, 1.0] into [0, 1]
    raw = num / den
    return max(0.0, min(1.0, (raw + 0.2) / 1.2))


def _top_terms(posts: list[XPost], n: int = 8) -> list[tuple[str, int]]:
    c: Counter[str] = Counter()
    for p in posts:
        for tok in _TOKEN.findall(p.text.lower()):
            if tok in _STOP or tok.isdigit():
                continue
            c[tok] += 1
    return c.most_common(n)


def rollup(posts: list[XPost], window: str, now: datetime | None = None) -> TrendRollup:
    now = now or datetime.now(timezone.utc)
    if window == "daily":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
    else:
        start = now.replace(minute=0, second=0, microsecond=0)
        end = start + timedelta(hours=1)
        window = "hourly"
    in_win = [p for p in posts if start <= p.created_at < end]
    # If nothing falls in the current bucket, use the latest bucket that has data
    if not in_win and posts:
        latest = max(p.created_at for p in posts)
        start = _bucket_start(latest, window)
        end = start + (timedelta(days=1) if window == "daily" else timedelta(hours=1))
        in_win = [p for p in posts if start <= p.created_at < end]
    counts: dict[str, int] = {"peg-stress": 0, "regulatory": 0, "bullish-mint": 0, "noise": 0}
    for p in in_win:
        counts[p.label] = counts.get(p.label, 0) + 1
    return TrendRollup(
        window=window,
        period_start=start,
        period_end=end,
        n_posts=len(in_win),
        counts=counts,
        fear_score=_fear(counts, in_win),
        top_terms=_top_terms(in_win),
    )
