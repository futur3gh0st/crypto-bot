from stablebot.market.depeg import deviation_bps, find_depegs
from tests.helpers import make_cfg, q


def test_deviation_math():
    assert abs(deviation_bps(0.9970, 1.0) - (-30.0)) < 1e-9
    assert abs(deviation_bps(1.0040, 1.0) - 40.0) < 1e-9


def test_depeg_threshold():
    cfg = make_cfg()
    quiet = [q("kraken", "USDT", "USD", 0.9990, 0.9992)]  # ~9 bps
    stressed = [q("kraken", "USDT", "USD", 0.9940, 0.9942)]  # ~59 bps
    assert find_depegs(quiet, cfg) == []
    alerts = find_depegs(stressed, cfg)
    assert len(alerts) == 1
    assert alerts[0].deviation_bps < -30


def test_eurc_usd_not_treated_as_dollar_peg():
    cfg = make_cfg()
    # EURC/USD around 1.08 is FX, not a depeg vs $1
    quotes = [q("coinbase", "EURC", "USD", 1.079, 1.081)]
    assert find_depegs(quotes, cfg) == []
