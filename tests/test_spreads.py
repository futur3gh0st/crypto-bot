from stablebot.market.spreads import (
    find_cross_pair,
    find_cross_venue,
    find_opportunities,
    gross_edge_bps,
    net_edge_bps,
)
from tests.helpers import make_cfg, q


def test_net_edge_positive_after_fees():
    # buy 0.9970 + 10bps, sell 1.0015 - 10bps
    net = net_edge_bps(0.9970, 1.0015, 10.0, 10.0)
    assert net > 8.0
    assert net < 40.0


def test_fee_negative_filtered():
    # 5 bps gross, 20 bps fees -> negative
    gross = gross_edge_bps(1.0000, 1.0005)
    assert 4.9 < gross < 5.1
    net = net_edge_bps(1.0000, 1.0005, 10.0, 10.0)
    assert net < 0
    cfg = make_cfg()
    quotes = [
        q("binance", "USDC", "USDT", 0.9999, 1.0000),
        q("bybit", "USDC", "USDT", 1.0004, 1.0006),
    ]
    assert find_cross_venue(quotes, cfg) == []


def test_cross_venue_emits_when_edge_clears_buffer():
    cfg = make_cfg()
    quotes = [
        q("binance", "USDC", "USDT", 0.9968, 0.9970),
        q("bybit", "USDC", "USDT", 1.0015, 1.0017),
    ]
    opps = find_cross_venue(quotes, cfg)
    assert len(opps) == 1
    assert opps[0].buy_venue == "binance"
    assert opps[0].sell_venue == "bybit"
    assert opps[0].net_bps >= 8.0


def test_coinbase_high_fees_kill_same_spread():
    cfg = make_cfg()
    quotes = [
        q("coinbase", "USDC", "USD", 0.9968, 0.9970),
        q("binance", "USDC", "USDT", 1.0015, 1.0017),
    ]
    # coinbase 60bps taker + binance 10bps usually wipes ~35bps gross
    opps = find_opportunities(quotes, cfg)
    # may still exist if we buy binance sell coinbase the wrong way; buy coinbase should fail
    buy_cb = [o for o in opps if o.buy_venue == "coinbase"]
    assert buy_cb == []


def test_cross_pair_usdt_vs_usdc():
    cfg = make_cfg()
    quotes = [
        q("binance", "USDT", "USD", 0.9960, 0.9962),
        q("binance", "USDC", "USD", 1.0010, 1.0012),
    ]
    opps = find_cross_pair(quotes, cfg)
    assert any(o.buy_base == "USDT" and o.sell_base == "USDC" for o in opps)


def test_split_usdtusd_not_usd_tusd():
    from stablebot.exchanges.base import split_concat_symbol

    assets = ["USDT", "USDC", "TUSD", "USD", "DAI"]
    assert split_concat_symbol("USDTUSD", assets) == ("USDT", "USD")
    assert split_concat_symbol("USDCUSDT", assets) == ("USDC", "USDT")
    assert split_concat_symbol("TUSDUSDT", assets) == ("TUSD", "USDT")
