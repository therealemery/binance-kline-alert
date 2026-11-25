from decimal import Decimal
from pathlib import Path

from binance_alert.models import Kline, PatternSignal, PatternType
from binance_alert.store import StateStore


def test_store_symbols_and_seed_once(tmp_path: Path):
    store = StateStore(tmp_path / "state.sqlite3")
    assert store.seed_symbols_if_empty(["BTCUSDT", "NVDABUSDT"]) == 2
    assert [s.symbol for s in store.list_symbols()] == ["BTCUSDT", "NVDABUSDT"]

    # Removing a symbol should not be undone by subsequent seed calls
    assert store.remove_symbol("NVDA") == "NVDABUSDT"
    assert store.seed_symbols_if_empty(["BTCUSDT", "NVDABUSDT"]) == 0
    assert [s.symbol for s in store.list_symbols()] == ["BTCUSDT"]

    assert store.add_symbol("ETHUSDT", added_by="alice") is True
    assert store.add_symbol("ETHUSDT", added_by="alice") is False
    assert store.remove_symbol("ETH") == "ETHUSDT"


def test_store_dedup_history_and_meta(tmp_path: Path):
    store = StateStore(tmp_path / "state.sqlite3")
    prev = Kline("BTCUSDT", "1h", 1000, 2000, Decimal("100"), Decimal("101"), Decimal("98"), Decimal("99"), Decimal("10"))
    curr = Kline("BTCUSDT", "1h", 2001, 3000, Decimal("98.5"), Decimal("103"), Decimal("98"), Decimal("102"), Decimal("25"))
    sig = PatternSignal("BTCUSDT", "BTC/USDT", "1h", PatternType.BULLISH_ENGULFING, prev, curr, Decimal("3.5"), Decimal("2.5"))

    assert store.get_last_alert_open_time("BTCUSDT", "1h") is None
    store.record_alert(sig, delivered_webhook=True, delivered_aibot=False)
    assert store.get_last_alert_open_time("BTCUSDT", "1h") == 2001
    assert store.get_total_alert_count() == 1

    history = store.get_recent_history(5)
    assert len(history) == 1
    assert history[0]["symbol"] == "BTCUSDT"

    assert store.is_paused() is False
    store.set_paused(True)
    assert store.is_paused() is True

    store.register_chat_id("wr_group_1")
    store.register_chat_id("wr_group_1")
    assert store.get_chat_ids() == ["wr_group_1"]
