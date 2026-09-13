from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from stablebot.config import data_dir
from stablebot.market.book import iso
from stablebot.market.depeg import DepegAlert
from stablebot.market.spreads import SpreadOpportunity


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


@dataclass
class Fill:
    id: int
    ts: str
    kind: str
    pair: str
    buy_venue: str
    sell_venue: str
    buy_px: float
    sell_px: float
    notional: float
    net_bps: float
    fees_paid: float
    pnl: float
    size_mult: float
    reason: str


class Ledger:
    def __init__(self, path: Path | None = None):
        self.path = path or (data_dir() / "ledger.sqlite")
        self._init()

    def _init(self) -> None:
        with _connect(self.path) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS quotes (
                    id INTEGER PRIMARY KEY,
                    ts TEXT NOT NULL,
                    venue TEXT, pair TEXT, bid REAL, ask REAL, mid REAL, last REAL
                );
                CREATE TABLE IF NOT EXISTS spreads (
                    id INTEGER PRIMARY KEY,
                    ts TEXT NOT NULL,
                    kind TEXT, pair TEXT,
                    buy_venue TEXT, sell_venue TEXT,
                    buy_px REAL, sell_px REAL,
                    gross_bps REAL, fee_bps REAL, net_bps REAL
                );
                CREATE TABLE IF NOT EXISTS depegs (
                    id INTEGER PRIMARY KEY,
                    ts TEXT NOT NULL,
                    venue TEXT, pair TEXT, mid REAL, deviation_bps REAL
                );
                CREATE TABLE IF NOT EXISTS fills (
                    id INTEGER PRIMARY KEY,
                    ts TEXT NOT NULL,
                    kind TEXT, pair TEXT,
                    buy_venue TEXT, sell_venue TEXT,
                    buy_px REAL, sell_px REAL,
                    notional REAL, net_bps REAL,
                    fees_paid REAL, pnl REAL,
                    size_mult REAL, reason TEXT
                );
                CREATE TABLE IF NOT EXISTS x_posts (
                    id INTEGER PRIMARY KEY,
                    ts TEXT NOT NULL,
                    tweet_id TEXT, username TEXT, label TEXT,
                    score REAL, likes INTEGER, retweets INTEGER, text TEXT
                );
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
                """
            )

    def record_quotes(self, quotes: Iterable[Any]) -> None:
        rows = []
        for q in quotes:
            mid = q.mid
            rows.append(
                (iso(q.ts), q.venue, q.pair, q.bid, q.ask, mid, q.last)
            )
        with _connect(self.path) as conn:
            conn.executemany(
                "INSERT INTO quotes (ts, venue, pair, bid, ask, mid, last) VALUES (?,?,?,?,?,?,?)",
                rows,
            )

    def record_spreads(self, opps: Iterable[SpreadOpportunity]) -> None:
        rows = [
            (
                iso(o.ts), o.kind, o.pair, o.buy_venue, o.sell_venue,
                o.buy_px, o.sell_px, o.gross_bps, o.fee_bps, o.net_bps,
            )
            for o in opps
        ]
        with _connect(self.path) as conn:
            conn.executemany(
                """INSERT INTO spreads
                   (ts, kind, pair, buy_venue, sell_venue, buy_px, sell_px, gross_bps, fee_bps, net_bps)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                rows,
            )

    def record_depegs(self, alerts: Iterable[DepegAlert]) -> None:
        rows = [(iso(a.ts), a.venue, a.pair, a.mid, a.deviation_bps) for a in alerts]
        with _connect(self.path) as conn:
            conn.executemany(
                "INSERT INTO depegs (ts, venue, pair, mid, deviation_bps) VALUES (?,?,?,?,?)",
                rows,
            )

    def record_fill(
        self,
        opp: SpreadOpportunity,
        notional: float,
        pnl: float,
        fees_paid: float,
        size_mult: float,
        reason: str,
        ts: datetime | None = None,
    ) -> int:
        when = iso(ts or opp.ts)
        with _connect(self.path) as conn:
            cur = conn.execute(
                """INSERT INTO fills
                   (ts, kind, pair, buy_venue, sell_venue, buy_px, sell_px,
                    notional, net_bps, fees_paid, pnl, size_mult, reason)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    when, opp.kind, opp.pair, opp.buy_venue, opp.sell_venue,
                    opp.buy_px, opp.sell_px, notional, opp.net_bps,
                    fees_paid, pnl, size_mult, reason,
                ),
            )
            return int(cur.lastrowid)

    def record_x_posts(self, posts: Iterable[Any]) -> None:
        rows = [
            (
                iso(p.created_at), p.id, p.username, p.label,
                p.score, p.likes, p.retweets, p.text[:500],
            )
            for p in posts
        ]
        with _connect(self.path) as conn:
            conn.executemany(
                """INSERT INTO x_posts
                   (ts, tweet_id, username, label, score, likes, retweets, text)
                   VALUES (?,?,?,?,?,?,?,?)""",
                rows,
            )

    def note(self, key: str, value: str) -> None:
        with _connect(self.path) as conn:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def get_note(self, key: str) -> str | None:
        with _connect(self.path) as conn:
            row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return row["value"] if row else None

    def fills_since(self, start: datetime) -> list[Fill]:
        with _connect(self.path) as conn:
            rows = conn.execute(
                "SELECT * FROM fills WHERE ts >= ? ORDER BY ts",
                (iso(start),),
            ).fetchall()
        return [self._fill(r) for r in rows]

    def spreads_since(self, start: datetime) -> list[sqlite3.Row]:
        with _connect(self.path) as conn:
            return list(
                conn.execute("SELECT * FROM spreads WHERE ts >= ? ORDER BY net_bps DESC", (iso(start),))
            )

    def depegs_since(self, start: datetime) -> list[sqlite3.Row]:
        with _connect(self.path) as conn:
            return list(
                conn.execute("SELECT * FROM depegs WHERE ts >= ? ORDER BY ts DESC", (iso(start),))
            )

    def x_since(self, start: datetime) -> list[sqlite3.Row]:
        with _connect(self.path) as conn:
            return list(
                conn.execute("SELECT * FROM x_posts WHERE ts >= ? ORDER BY score DESC", (iso(start),))
            )

    def pnl_since(self, start: datetime) -> float:
        with _connect(self.path) as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(pnl), 0) AS s FROM fills WHERE ts >= ?",
                (iso(start),),
            ).fetchone()
        return float(row["s"])

    @staticmethod
    def _fill(r: sqlite3.Row) -> Fill:
        return Fill(
            id=r["id"], ts=r["ts"], kind=r["kind"], pair=r["pair"],
            buy_venue=r["buy_venue"], sell_venue=r["sell_venue"],
            buy_px=r["buy_px"], sell_px=r["sell_px"],
            notional=r["notional"], net_bps=r["net_bps"],
            fees_paid=r["fees_paid"], pnl=r["pnl"],
            size_mult=r["size_mult"], reason=r["reason"],
        )


def window_start(window: str, now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    if window == "daily":
        return now - timedelta(days=1)
    return now - timedelta(hours=1)
