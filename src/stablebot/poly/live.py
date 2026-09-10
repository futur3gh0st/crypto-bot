"""Gated Polymarket CLOB V2 pair-complete. Paper remains the default.

Never imported by the paper path. Fade / complete_hedge are not implemented here.
A real POST /order requires every independent gate to pass and dry_run=False.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rich.console import Console

from stablebot.config import PolyCfg, data_dir
from stablebot.poly.client import BestLevel, fetch_best_book_sync
from stablebot.poly.markets import ScanRow
from stablebot.poly.paper import PaperFill, PolyLedger
from stablebot.poly.replay import poly_taker_fee
from stablebot.poly.strategy import lock_edge

console = Console(width=200)

CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137
CONFIRM_NAME = "poly_live_confirm.txt"
CONFIRM_TEXT = "I_ACCEPT_LIVE_POLY_ORDERS"
HALT_NAME = "poly_halt"
PUSD_DECIMALS = 1_000_000.0

REQUIRED_CLOB_ENV = ("CLOB_API_KEY", "CLOB_SECRET", "CLOB_PASS_PHRASE")
OPTIONAL_ENV = ("POLY_FUNDER", "POLY_SIGNATURE_TYPE", "POLY_BUILDER_CODE")
SIG_LABELS = {
    0: "EOA",
    1: "POLY_PROXY",
    2: "POLY_GNOSIS_SAFE",
    3: "POLY_1271",
}


def _iso(ts: datetime | None = None) -> str:
    return (ts or datetime.now(timezone.utc)).isoformat()


def confirm_path() -> Path:
    return data_dir() / CONFIRM_NAME


def halt_path() -> Path:
    return data_dir() / HALT_NAME


def halt_present() -> bool:
    return halt_path().exists()


def confirm_ok() -> bool:
    path = confirm_path()
    if not path.is_file():
        return False
    return path.read_text(encoding="utf-8").strip() == CONFIRM_TEXT


def confirm_status() -> str:
    path = confirm_path()
    if not path.is_file():
        return "missing"
    if path.read_text(encoding="utf-8").strip() == CONFIRM_TEXT:
        return "ok"
    return "present but contents do not equal I_ACCEPT_LIVE_POLY_ORDERS"


def poly_live_env_on() -> bool:
    """Only POLY_LIVE=1. CEX LIVE=1 does not count."""
    return os.environ.get("POLY_LIVE", "").strip() == "1"


def private_key() -> str | None:
    for name in ("POLY_PK", "PK"):
        raw = os.environ.get(name, "").strip()
        if raw:
            return raw
    return None


def private_key_name() -> str | None:
    if os.environ.get("POLY_PK", "").strip():
        return "POLY_PK"
    if os.environ.get("PK", "").strip():
        return "PK"
    return None


def env_present(name: str) -> bool:
    return bool(os.environ.get(name, "").strip())


def missing_env_names() -> list[str]:
    missing: list[str] = []
    if private_key() is None:
        missing.append("POLY_PK")
    for name in REQUIRED_CLOB_ENV:
        if not env_present(name):
            missing.append(name)
    return missing


def clob_v2_importable() -> bool:
    try:
        import py_clob_client_v2  # noqa: F401
    except ImportError:
        return False
    return True


def builder_code() -> str | None:
    raw = os.environ.get("POLY_BUILDER_CODE", "").strip()
    return raw or None


def signature_type() -> int | None:
    raw = os.environ.get("POLY_SIGNATURE_TYPE", "").strip()
    if not raw:
        return None
    return int(raw)


def funder_address() -> str | None:
    raw = os.environ.get("POLY_FUNDER", "").strip()
    return raw or None


def live_gates_ok(*, cli_live: bool, fade: bool) -> tuple[bool, list[str]]:
    """Startup gates 1-7. Fail closed."""
    fails: list[str] = []
    if not cli_live:
        fails.append("CLI --live / --live-dry-run not set")
    if fade:
        fails.append("--fade is forbidden with --live")
    if not poly_live_env_on():
        fails.append("POLY_LIVE is not 1")
    if not confirm_ok():
        fails.append(
            f"confirm file {CONFIRM_NAME} missing or contents != {CONFIRM_TEXT}"
        )
    if halt_present():
        fails.append(f"halt file {HALT_NAME} is present")
    if not clob_v2_importable():
        fails.append("py_clob_client_v2 is not importable")
    missing = missing_env_names()
    if missing:
        fails.append("missing env: " + ", ".join(missing))
    return (len(fails) == 0, fails)


def require_live_ready(*, cli_live: bool, fade: bool) -> None:
    ok, fails = live_gates_ok(cli_live=cli_live, fade=fade)
    if not ok:
        raise SystemExit(
            "live gates failed — refusing to start (will not fall back to paper):\n  - "
            + "\n  - ".join(fails)
        )


def _as_dict(resp: Any) -> dict[str, Any]:
    if resp is None:
        return {}
    if isinstance(resp, dict):
        return resp
    if hasattr(resp, "__dict__"):
        try:
            return dict(resp.__dict__)
        except Exception:  # noqa: BLE001
            return {"repr": repr(resp)}
    return {"repr": repr(resp)}


def order_filled(resp: Any) -> bool:
    data = _as_dict(resp)
    if not data:
        return False
    if data.get("success") is False:
        return False
    err = data.get("error") or data.get("errorMsg") or data.get("error_msg")
    if err:
        return False
    status = str(data.get("status") or "").lower()
    if status in {"killed", "cancelled", "canceled", "rejected", "unmatched", "live", "delayed"}:
        return False
    if status in {"matched", "filled", "ok"}:
        return True
    if data.get("success") is True:
        return True
    if data.get("tradeIDs") or data.get("trade_ids"):
        return True
    return False


def parse_fill(resp: Any, fallback_price: float, fallback_size: float) -> tuple[float, float]:
    data = _as_dict(resp)
    size = fallback_size
    for key in ("size_matched", "filledSize", "filled_size", "takingAmount", "taking_amount"):
        raw = data.get(key)
        if raw is None:
            continue
        try:
            val = float(raw)
        except (TypeError, ValueError):
            continue
        if val > 0:
            size = val
            break
    price = fallback_price
    making = data.get("makingAmount") or data.get("making_amount")
    taking = data.get("takingAmount") or data.get("taking_amount")
    try:
        if making is not None and taking is not None:
            m = float(making)
            t = float(taking)
            if t > 0 and 0 < m / t < 1:
                price = m / t
            elif m > 0 and 0 < t / m < 1:
                price = t / m
    except (TypeError, ValueError, ZeroDivisionError):
        price = fallback_price
    raw_px = data.get("price") or data.get("avgPrice") or data.get("avg_price")
    if raw_px is not None:
        try:
            px = float(raw_px)
            if 0 < px < 1:
                price = px
        except (TypeError, ValueError):
            pass
    return price, size


def _parse_ts(raw: Any) -> datetime | None:
    if not raw:
        return None
    text = str(raw).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class ClobLive:
    """Lazy py_clob_client_v2 wrapper. Constructed only after startup gates pass."""

    def __init__(self) -> None:
        self._client: Any = None
        self._sdk: dict[str, Any] | None = None

    def _load_sdk(self) -> dict[str, Any]:
        if self._sdk is None:
            from py_clob_client_v2 import (
                ApiCreds,
                AssetType,
                BalanceAllowanceParams,
                ClobClient,
                MarketOrderArgs,
                OrderArgs,
                OrderType,
                PartialCreateOrderOptions,
                Side,
            )

            self._sdk = {
                "ApiCreds": ApiCreds,
                "AssetType": AssetType,
                "BalanceAllowanceParams": BalanceAllowanceParams,
                "ClobClient": ClobClient,
                "MarketOrderArgs": MarketOrderArgs,
                "OrderArgs": OrderArgs,
                "OrderType": OrderType,
                "PartialCreateOrderOptions": PartialCreateOrderOptions,
                "Side": Side,
            }
        return self._sdk

    def client(self) -> Any:
        if self._client is None:
            sdk = self._load_sdk()
            pk = private_key()
            if not pk:
                raise RuntimeError("POLY_PK (or PK) missing")
            creds = sdk["ApiCreds"](
                api_key=os.environ["CLOB_API_KEY"].strip(),
                api_secret=os.environ["CLOB_SECRET"].strip(),
                api_passphrase=os.environ["CLOB_PASS_PHRASE"].strip(),
            )
            kwargs: dict[str, Any] = {
                "host": CLOB_HOST,
                "chain_id": CHAIN_ID,
                "key": pk,
                "creds": creds,
            }
            sig = signature_type()
            if sig is not None:
                kwargs["signature_type"] = sig
            funder = funder_address()
            if funder:
                kwargs["funder"] = funder
            self._client = sdk["ClobClient"](**kwargs)
        return self._client

    def _order_kwargs(self, token_id: str, price: float, size: float, side: Any) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "token_id": token_id,
            "price": price,
            "size": size,
            "side": side,
        }
        code = builder_code()
        if code:
            kwargs["builder_code"] = code
        return kwargs

    def post_fok_buy(self, token_id: str, price: float, size: float) -> Any:
        sdk = self._load_sdk()
        try:
            return self.client().create_and_post_order(
                order_args=sdk["OrderArgs"](
                    **self._order_kwargs(token_id, price, size, sdk["Side"].BUY)
                ),
                options=sdk["PartialCreateOrderOptions"](),
                order_type=sdk["OrderType"].FOK,
            )
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "errorMsg": f"{type(exc).__name__}: {exc}"}

    def post_fok_sell(self, token_id: str, price: float, size: float) -> Any:
        sdk = self._load_sdk()
        try:
            return self.client().create_and_post_order(
                order_args=sdk["OrderArgs"](
                    **self._order_kwargs(token_id, price, size, sdk["Side"].SELL)
                ),
                options=sdk["PartialCreateOrderOptions"](),
                order_type=sdk["OrderType"].FOK,
            )
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "errorMsg": f"{type(exc).__name__}: {exc}"}

    def post_fok_market_sell(self, token_id: str, size: float) -> Any:
        sdk = self._load_sdk()
        kwargs: dict[str, Any] = {
            "token_id": token_id,
            "amount": size,
            "side": sdk["Side"].SELL,
            "order_type": sdk["OrderType"].FOK,
        }
        code = builder_code()
        if code:
            kwargs["builder_code"] = code
        try:
            return self.client().create_and_post_market_order(
                order_args=sdk["MarketOrderArgs"](**kwargs),
                options=sdk["PartialCreateOrderOptions"](),
                order_type=sdk["OrderType"].FOK,
            )
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "errorMsg": f"{type(exc).__name__}: {exc}"}

    def collateral_snapshot(self) -> dict[str, Any] | None:
        client = self.client()
        fn = getattr(client, "get_balance_allowance", None)
        if fn is None:
            return None
        sdk = self._load_sdk()
        try:
            raw = fn(sdk["BalanceAllowanceParams"](asset_type=sdk["AssetType"].COLLATERAL))
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(raw, dict) or "balance" not in raw:
            return None
        try:
            balance = int(raw["balance"]) / PUSD_DECIMALS
        except (TypeError, ValueError):
            try:
                balance = float(raw["balance"]) / PUSD_DECIMALS
            except (TypeError, ValueError):
                return None
        allowance_raw = raw.get("allowance")
        allowance = None
        if allowance_raw is not None:
            try:
                allowance = int(allowance_raw) / PUSD_DECIMALS
            except (TypeError, ValueError):
                try:
                    allowance = float(allowance_raw) / PUSD_DECIMALS
                except (TypeError, ValueError):
                    allowance = None
        return {"balance": balance, "allowance": allowance, "raw": raw}

    def collateral_balance(self) -> float | None:
        snap = self.collateral_snapshot()
        if snap is None:
            return None
        return float(snap["balance"])


@dataclass
class _Books:
    up: BestLevel
    down: BestLevel


class PolyLive:
    """Live / dry-run pair-complete. No fade. One-leg unwind is mandatory."""

    def __init__(
        self,
        cfg: PolyCfg,
        ledger: PolyLedger | None = None,
        clob: ClobLive | None = None,
        dry_run: bool = False,
        book_fetch: Any = None,
    ):
        self.cfg = cfg
        self.ledger = ledger or PolyLedger()
        self.clob = clob or ClobLive()
        self.dry_run = dry_run
        self._book_fetch = book_fetch or fetch_best_book_sync
        self.completed: set[str] = set()
        self.halted = False
        self._replay()

    def _replay(self) -> None:
        for rec in self.ledger.load():
            slug = str(rec.get("slug") or "")
            kind = rec.get("kind")
            if slug and kind in {"pair_complete", "one_leg_unwind"}:
                self.completed.add(slug)

    def step(self, rows: list[ScanRow], now: datetime | None = None) -> list[PaperFill]:
        fills: list[PaperFill] = []
        for row in rows:
            if self.halted or halt_present():
                self.halted = True
                fills.append(
                    PaperFill(
                        "halt",
                        row.slug,
                        0.0,
                        0.0,
                        "halt file present; live sending stopped (scan only)",
                        True,
                    )
                )
                break
            fills.extend(self.try_pair_complete(row, now))
        return fills

    def try_pair_complete(self, row: ScanRow, now: datetime | None = None) -> list[PaperFill]:
        if halt_present():
            self.halted = True
            return [
                PaperFill(
                    "halt",
                    row.slug,
                    0.0,
                    0.0,
                    "halt file present; live sending stopped (scan only)",
                    True,
                )
            ]
        if row.slug in self.completed:
            return [
                PaperFill("pair_complete", row.slug, 0.0, 0.0, "already pair-complete", True)
            ]
        if not row.up_token_id or not row.down_token_id:
            return [self._skip(row, "missing token ids", now)]

        shares = float(self.cfg.paper_shares)
        if shares <= 0:
            return [self._skip(row, "shares <= 0", now)]
        if shares > float(self.cfg.live_max_shares):
            return [
                self._skip(
                    row,
                    f"shares {shares:g} > live_max_shares {self.cfg.live_max_shares:g}",
                    now,
                )
            ]

        try:
            books = _Books(
                up=self._book_fetch(row.up_token_id),
                down=self._book_fetch(row.down_token_id),
            )
        except Exception as exc:  # noqa: BLE001
            return [self._skip(row, f"book fetch failed: {type(exc).__name__}: {exc}", now)]

        ask_up, sz_up = books.up.ask, books.up.ask_size
        ask_down, sz_down = books.down.ask, books.down.ask_size
        bid_up, bid_down = books.up.bid, books.down.bid
        if ask_up is None or ask_down is None:
            return [self._skip(row, "missing best ask", now)]
        if bid_up is None or bid_down is None or bid_up <= 0 or bid_down <= 0:
            return [self._skip(row, "missing best bid", now)]
        if bid_up > ask_up or bid_down > ask_down:
            return [self._skip(row, "crossed book (bid > ask)", now)]
        size_mult = float(self.cfg.live_ask_size_mult)
        need_sz = shares * size_mult
        if sz_up is None or sz_down is None or sz_up < need_sz or sz_down < need_sz:
            return [
                self._skip(
                    row,
                    f"best-ask size < {size_mult:g}x shares "
                    f"(up={sz_up} down={sz_down} need={need_sz:g})",
                    now,
                )
            ]

        edge = lock_edge(ask_up, ask_down, self.cfg.taker_fee_bps)
        if edge is None:
            return [self._skip(row, "no lock edge", now)]
        curve = poly_taker_fee(float(ask_up)) + poly_taker_fee(float(ask_down))
        after = edge - curve
        if after <= self.cfg.min_lock:
            return [
                self._skip(
                    row,
                    f"min_lock fails after curve: {after:.4f} <= {self.cfg.min_lock}",
                    now,
                )
            ]

        unwind_up = float(ask_up) - float(bid_up)
        unwind_down = float(ask_down) - float(bid_down)
        if unwind_up >= after or unwind_down >= after:
            return [
                self._skip(
                    row,
                    "unwind spread would wipe lock "
                    f"(up={unwind_up:.4f} down={unwind_down:.4f} after={after:.4f})",
                    now,
                )
            ]

        cost = shares * (float(ask_up) + float(ask_down))
        need = cost * (1.0 + float(self.cfg.live_balance_buffer))
        spent = self._today_live_notional(now)
        if spent + cost > float(self.cfg.live_daily_notional):
            return [
                self._skip(
                    row,
                    f"daily notional {spent:.2f}+{cost:.2f} > {self.cfg.live_daily_notional}",
                    now,
                )
            ]

        bal = self.clob.collateral_balance()
        if bal is None:
            return [
                self._skip(
                    row,
                    "pUSD balance API missing or failed; refuse live rather than guess",
                    now,
                )
            ]
        if bal < need:
            return [self._skip(row, f"pUSD {bal:.4f} < need {need:.4f}", now)]

        would = {
            "ts": _iso(now),
            "kind": "live_dry_run" if self.dry_run else "pair_complete",
            "slug": row.slug,
            "coin": row.coin,
            "minutes": row.minutes,
            "which": row.which,
            "ask_up": ask_up,
            "ask_down": ask_down,
            "up_ask_size": sz_up,
            "down_ask_size": sz_down,
            "up_token_id": row.up_token_id,
            "down_token_id": row.down_token_id,
            "curve_fee": curve,
            "lock_edge": edge,
            "lock_after_curve": after,
            "shares": shares,
            "cost": cost,
            "live": False if self.dry_run else True,
        }

        # Thinner best-ask first (tie: Up, same as the previous hardcoded order).
        if float(sz_down) < float(sz_up):
            first_side, second_side = "down", "up"
            first_token, second_token = row.down_token_id, row.up_token_id
            first_ask, second_ask = float(ask_down), float(ask_up)
            first_bid = bid_down
        else:
            first_side, second_side = "up", "down"
            first_token, second_token = row.up_token_id, row.down_token_id
            first_ask, second_ask = float(ask_up), float(ask_down)
            first_bid = bid_up
        first_label = first_side.capitalize()
        second_label = second_side.capitalize()
        would["first_side"] = first_side

        if self.dry_run:
            would["note"] = (
                f"dry-run; would FOK BUY {first_label} then {second_label}; not posted"
            )
            console.print(
                f"DRY-RUN would FOK BUY {first_label} {shares:g} @ {first_ask:.4f} then "
                f"{second_label} {shares:g} @ {second_ask:.4f} slug={row.slug}"
            )
            return [
                PaperFill(
                    "live_dry_run",
                    row.slug,
                    shares,
                    shares * after,
                    f"dry-run lock {after:.4f} / share (not posted); "
                    f"{first_label} first",
                    True,
                    would,
                )
            ]

        first_resp = self.clob.post_fok_buy(first_token, first_ask, shares)
        if not order_filled(first_resp):
            note = f"{first_label} FOK failed; {second_label} not sent"
            rec = {
                **would,
                "kind": "pair_skip",
                "live": True,
                f"{first_side}_resp": _as_dict(first_resp),
                "note": note,
            }
            self.ledger.append(rec)
            return [
                PaperFill(
                    "pair_skip",
                    row.slug,
                    0.0,
                    0.0,
                    note,
                    True,
                    rec,
                )
            ]

        fill_first, fill_first_sz = parse_fill(first_resp, first_ask, shares)
        second_resp = self.clob.post_fok_buy(second_token, second_ask, shares)
        if first_side == "up":
            up_resp, down_resp = first_resp, second_resp
            fill_up, fill_up_sz = fill_first, fill_first_sz
        else:
            down_resp, up_resp = first_resp, second_resp
            fill_down, fill_down_sz = fill_first, fill_first_sz

        if not order_filled(second_resp):
            unwind_ok, unwind_resp, residual = self._unwind(
                first_token, fill_first_sz, first_bid
            )
            note = f"{second_label} FOK failed; sold {first_label}"
            if residual or not unwind_ok:
                note = (
                    f"RESIDUAL RISK: {second_label} FOK failed and {first_label} sell "
                    f"failed; {first_label} position may still be open"
                )
            rec = {
                **would,
                "kind": "one_leg_unwind",
                "live": True,
                "shares": fill_first_sz,
                "fill_px": fill_first,
                "up_resp": _as_dict(up_resp),
                "down_resp": _as_dict(down_resp),
                "unwind_resp": _as_dict(unwind_resp),
                "unwind_ok": unwind_ok,
                "residual_risk": bool(residual or not unwind_ok),
                "note": note,
            }
            if first_side == "up":
                rec["ask_up"] = fill_first
            else:
                rec["ask_down"] = fill_first
            self.ledger.append(rec)
            self.completed.add(row.slug)
            return [
                PaperFill(
                    "one_leg_unwind",
                    row.slug,
                    fill_first_sz,
                    0.0,
                    note,
                    False,
                    rec,
                )
            ]

        fill_second, fill_second_sz = parse_fill(second_resp, second_ask, shares)
        if first_side == "up":
            fill_down, fill_down_sz = fill_second, fill_second_sz
        else:
            fill_up, fill_up_sz = fill_second, fill_second_sz
        used = min(fill_up_sz, fill_down_sz)
        curve_actual = poly_taker_fee(fill_up) + poly_taker_fee(fill_down)
        flat = self.cfg.taker_fee_bps / 10_000.0
        pnl = used * (1.0 - fill_up - fill_down - flat - curve_actual)
        rec = {
            **would,
            "kind": "pair_complete",
            "live": True,
            "ask_up": fill_up,
            "ask_down": fill_down,
            "shares": used,
            "curve_fee": curve_actual,
            "pnl": pnl,
            "up_resp": _as_dict(up_resp),
            "down_resp": _as_dict(down_resp),
            "note": f"live pair-complete FOK both legs ({first_label} first)",
        }
        self.ledger.append(rec)
        self.completed.add(row.slug)
        return [
            PaperFill(
                "pair_complete",
                row.slug,
                used,
                pnl,
                f"live lock {1.0 - fill_up - fill_down - flat - curve_actual:.4f} / share",
                False,
                rec,
            )
        ]

    def _unwind(
        self, token_id: str, shares: float, stale_bid: float | None
    ) -> tuple[bool, Any, bool]:
        bid = stale_bid
        try:
            fresh = self._book_fetch(token_id)
            if fresh.bid is not None:
                bid = fresh.bid
        except Exception:  # noqa: BLE001
            pass
        if bid is not None and bid > 0:
            resp = self.clob.post_fok_sell(token_id, float(bid), shares)
            if order_filled(resp):
                return True, resp, False
        resp = self.clob.post_fok_market_sell(token_id, shares)
        if order_filled(resp):
            return True, resp, False
        return False, resp, True

    def _today_live_notional(self, now: datetime | None) -> float:
        day = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).date()
        total = 0.0
        for rec in self.ledger.load():
            if not rec.get("live"):
                continue
            if rec.get("kind") not in {"pair_complete", "one_leg_unwind"}:
                continue
            ts = _parse_ts(rec.get("ts"))
            if ts is None or ts.date() != day:
                continue
            shares = float(rec.get("shares") or 0.0)
            if rec.get("kind") == "pair_complete":
                au = float(rec.get("ask_up") or 0.0)
                ad = float(rec.get("ask_down") or 0.0)
                total += shares * (au + ad)
            else:
                fill_px = rec.get("fill_px")
                if fill_px is not None:
                    total += shares * float(fill_px)
                else:
                    au = float(rec.get("ask_up") or 0.0)
                    ad = float(rec.get("ask_down") or 0.0)
                    total += shares * (au if au > 0 else ad)
        return total

    def _skip(self, row: ScanRow, reason: str, now: datetime | None) -> PaperFill:
        return PaperFill("pair_complete", row.slug, 0.0, 0.0, reason, True)


def cmd_poly_live_check() -> None:
    """Validate env / files / client. Never place an order. Never print secrets."""
    console.rule("[bold]poly-live-check (no orders)")
    console.print("Credentials checklist (names only; values never printed):")
    pk_name = private_key_name()
    if pk_name:
        console.print(f"  {pk_name}: set")
    else:
        console.print("  POLY_PK (or PK): MISSING")
    for name in REQUIRED_CLOB_ENV:
        console.print(f"  {name}: {'set' if env_present(name) else 'MISSING'}")
    for name in OPTIONAL_ENV:
        console.print(f"  {name}: {'set' if env_present(name) else 'unset (optional)'}")

    missing = missing_env_names()
    if missing:
        console.print("Missing required env: " + ", ".join(missing))
    else:
        console.print("Required env names: all present")

    console.print(f"POLY_LIVE==1: {'yes' if poly_live_env_on() else 'no'}")
    console.print("CEX LIVE is ignored for Polymarket (do not use LIVE=1).")
    console.print(f"confirm file {confirm_path()}: {confirm_status()}")
    console.print(f"confirm file must contain exactly: {CONFIRM_TEXT}")
    console.print(
        f"halt file {halt_path()}: {'PRESENT (live sending blocked)' if halt_present() else 'absent'}"
    )
    console.print(
        f"py_clob_client_v2 importable: {'yes' if clob_v2_importable() else 'no'}"
    )

    sig = None
    try:
        sig = signature_type()
    except ValueError:
        console.print("POLY_SIGNATURE_TYPE: present but not an int")
    if sig is None:
        console.print("signature type: unset (client default / EOA)")
    else:
        console.print(f"signature type: {sig} ({SIG_LABELS.get(sig, 'unknown')})")
    funder = funder_address()
    console.print(f"funder: {funder or '(none — EOA / signing key)'}")

    if not clob_v2_importable():
        console.print("Cannot construct CLOB client (package missing). No orders attempted.")
        return
    if private_key() is None or missing:
        console.print("Cannot construct CLOB client (required env missing). No orders attempted.")
        return

    clob = ClobLive()
    try:
        client = clob.client()
    except Exception as exc:  # noqa: BLE001
        console.print(f"CLOB client construct failed: {type(exc).__name__}: {exc}")
        console.print("No orders attempted.")
        return

    try:
        addr = client.get_address()
        console.print(f"address: {addr}")
    except Exception as exc:  # noqa: BLE001
        console.print(f"address: unavailable ({type(exc).__name__})")

    snap = None
    try:
        snap = clob.collateral_snapshot()
    except Exception as exc:  # noqa: BLE001
        console.print(f"pUSD balance/allowance: error {type(exc).__name__}")
        snap = None
    if snap is None:
        console.print(
            "pUSD balance/allowance: unavailable "
            "(get_balance_allowance missing or failed; live will refuse rather than guess)"
        )
    else:
        bal = snap["balance"]
        allow = snap["allowance"]
        console.print(f"pUSD balance: {bal:.4f}")
        if allow is None:
            console.print("pUSD allowance: unknown")
        else:
            console.print(f"pUSD allowance: {allow:.4f}")

    console.print("No orders placed. Paper remains the default until every live gate passes.")
