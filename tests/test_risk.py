from datetime import datetime, timezone

from stablebot.signals.trends import TrendRollup, classify_text, engagement_score, rollup
from stablebot.signals.x_client import XPost
from stablebot.strategy.risk import decide
from tests.helpers import make_cfg


def test_classify_keywords():
    assert classify_text("USDT depeg incoming") == "peg-stress"
    assert classify_text("SEC lawsuit against issuer") == "regulatory"
    assert classify_text("Circle attestation passed, fully backed") == "bullish-mint"
    assert classify_text("gm stables") == "noise"


def test_fear_skips_and_cuts():
    cfg = make_cfg()
    now = datetime(2026, 7, 1, 15, 0, tzinfo=timezone.utc)
    skip = TrendRollup("hourly", now, now, 4, {"peg-stress": 4}, 0.92, [])
    cut = TrendRollup("hourly", now, now, 4, {"regulatory": 3}, 0.70, [])
    ok = TrendRollup("hourly", now, now, 4, {"noise": 4}, 0.10, [])
    assert decide(cfg, skip).trade is False
    assert decide(cfg, cut).size_mult == cfg.risk.fear_size_mult
    assert decide(cfg, ok).size_mult == 1.0
    assert decide(cfg, None).trade is True


def test_rollup_hourly():
    ts = datetime(2026, 7, 1, 15, 10, tzinfo=timezone.utc)
    posts = [
        XPost("1", "USDT depeg", ts, likes=10, retweets=5),
        XPost("2", "hello stables", ts, likes=1),
    ]
    from stablebot.signals.trends import annotate

    annotate(posts)
    r = rollup(posts, "hourly", now=ts)
    assert r.n_posts == 2
    assert r.counts["peg-stress"] == 1
    assert engagement_score(posts[0]) > engagement_score(posts[1])


# ---------------------------------------------------------------------------
# clip vs daily stop — the ratio, not either number alone
# ---------------------------------------------------------------------------

MIN_CLIPS_OF_HEADROOM = 4.0


def test_the_daily_stop_absorbs_a_losing_run_not_one_clip():
    """A binary bought outright loses its whole stake, so the clip is the unit
    the daily stop is spent in. Shipped defaults once gave 1.1 clips of
    headroom: a single loss halted the desk before it could measure anything.
    """
    from stablebot.config import load_config
    from stablebot.desk.allocator import Allocator, AllocatorCfg
    from stablebot.desk.risk import RiskGovernor
    from stablebot.desk.state import DeskState, SleeveStat

    pot = 3000.0
    cfg = load_config()
    st = DeskState()
    st.starting_equity = pot
    st.equity = pot
    st.risk.day_start_equity = pot
    st.risk.daily_stop_pct = cfg.risk.daily_stop_pct
    RiskGovernor.update_budget(st)

    stat = SleeveStat("kl", "Kalshi Lag")
    stat.enabled = True
    st.sleeves = {"kl": stat}
    Allocator(AllocatorCfg(pot=pot)).rebalance(st, force=True)

    assert stat.clip > 0
    headroom = st.risk.budget_total / stat.clip
    assert headroom >= MIN_CLIPS_OF_HEADROOM, (
        f"daily stop {cfg.risk.daily_stop_pct:.1%} on a {pot:.0f} pot absorbs only "
        f"{headroom:.1f} losing clips of ${stat.clip:.2f} — raise the stop or cut the clip"
    )


# The session halt must sit this far above the daily stop for the two to do
# different jobs. Was 2.0 when the pair was 4%/10%; relaxed to 1.5 for the
# 16%/25% pair, which is a deliberately tighter separation — one very bad day
# now leaves 9 points of room before the session halt rather than 6.
MIN_TIER_RATIO = 1.5


def test_the_daily_stop_stays_clear_of_the_drawdown_halt():
    """Two tiers only work while they carry different magnitudes."""
    from stablebot.config import load_config
    from stablebot.desk.risk import RiskGovernor

    stop = load_config().risk.daily_stop_pct
    max_dd = RiskGovernor(load_config()).max_drawdown_pct
    assert max_dd >= stop * MIN_TIER_RATIO, (
        f"daily stop {stop:.1%} is not meaningfully tighter than the "
        f"{max_dd:.1%} drawdown halt — one limit is doing both jobs"
    )
