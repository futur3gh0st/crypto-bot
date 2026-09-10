from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

from stablebot.config import AppConfig, EnvSettings
from stablebot.exchanges.base import USER_AGENT

SEARCH_URLS = (
    "https://api.x.com/2/tweets/search/recent",
    "https://api.twitter.com/2/tweets/search/recent",
)


class XDisabled(Exception):
    """Raised once when no bearer token is configured."""


@dataclass
class XPost:
    id: str
    text: str
    created_at: datetime
    likes: int = 0
    retweets: int = 0
    replies: int = 0
    followers: int = 0
    username: str = ""
    label: str = "noise"
    score: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)


def _parse_dt(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)


class XClient:
    def __init__(self, token: str | None, cfg: AppConfig):
        self.token = (token or "").strip() or None
        self.cfg = cfg
        self._warned = False

    @property
    def enabled(self) -> bool:
        return bool(self.token) and self.cfg.x.enabled

    def disabled_message(self) -> str | None:
        if self.enabled:
            return None
        if self._warned:
            return None
        self._warned = True
        return "X disabled (no bearer token)"

    async def recent_search(self, client: httpx.AsyncClient) -> list[XPost]:
        if not self.enabled:
            return []
        params = {
            "query": self.cfg.x.query,
            "max_results": str(max(10, min(self.cfg.x.max_results, 100))),
            "tweet.fields": "created_at,public_metrics,lang",
            "expansions": "author_id",
            "user.fields": "public_metrics,username",
        }
        headers = {
            "Authorization": f"Bearer {self.token}",
            "User-Agent": USER_AGENT,
        }
        last_err: Exception | None = None
        payload = None
        for url in SEARCH_URLS:
            try:
                resp = await client.get(url, params=params, headers=headers)
                if resp.status_code in (401, 403):
                    raise RuntimeError(f"X API {resp.status_code}: check bearer token / paid tier")
                resp.raise_for_status()
                payload = resp.json()
                break
            except Exception as exc:  # noqa: BLE001
                last_err = exc
        if payload is None:
            raise last_err or RuntimeError("X recent search failed")

        users = {
            u.get("id"): u
            for u in ((payload.get("includes") or {}).get("users") or [])
        }
        posts: list[XPost] = []
        for row in payload.get("data") or []:
            metrics = row.get("public_metrics") or {}
            user = users.get(row.get("author_id")) or {}
            um = user.get("public_metrics") or {}
            posts.append(
                XPost(
                    id=str(row.get("id") or ""),
                    text=str(row.get("text") or ""),
                    created_at=_parse_dt(row.get("created_at")),
                    likes=int(metrics.get("like_count") or 0),
                    retweets=int(metrics.get("retweet_count") or 0),
                    replies=int(metrics.get("reply_count") or 0),
                    followers=int(um.get("followers_count") or 0),
                    username=str(user.get("username") or ""),
                    raw=row,
                )
            )
        return posts


def from_env(cfg: AppConfig, settings: EnvSettings | None = None) -> XClient:
    from stablebot.config import env_settings

    settings = settings or env_settings()
    return XClient(settings.x_bearer_token, cfg)
