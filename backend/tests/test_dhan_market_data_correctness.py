"""Dhan market-data correctness tests.

Packet fixtures are synthesized from the documented Dhan binary layouts. They
exercise parsing only and are not evidence of live connectivity.
"""

import struct
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from app.api import market_data
from app.broker_adapters import (
    BrokerAdapterError,
    BrokerFeedDisconnected,
    DhanAdapter,
    _dhan_candle_is_complete,
    _dhan_ist_epoch_to_utc,
)
from app.market_data import HistoricalBar, InstrumentCandidate
from app.provider_adapters import DhanV2Adapter

IST = ZoneInfo("Asia/Kolkata")
SBIN_SECURITY_ID = "3045"
SBIN_SEGMENT = "NSE_EQ"
# Documented sample: 2012-01-11 00:00 IST, encoded as IST wall-clock epoch.
DOCUMENTED_IST_DAILY_EPOCH = 1326220200


def _header(code: int, length: int, segment_code: int, security_id: int) -> bytearray:
    packet = bytearray(length)
    packet[0] = code
    struct.pack_into("<h", packet, 1, length)
    packet[3] = segment_code
    struct.pack_into("<i", packet, 4, security_id)
    return packet


def _ticker_packet(security_id: int = 3045, ltp: float = 987.5, ltt: int = 1700000000) -> bytes:
    packet = _header(2, 16, 1, security_id)
    struct.pack_into("<f", packet, 8, ltp)
    struct.pack_into("<i", packet, 12, ltt)
    return bytes(packet)


def _quote_packet(
    security_id: int = 3045,
    ltp: float = 987.5,
    ltt: int = 1700000000,
    volume: int = 1234567,
) -> bytes:
    packet = _header(4, 50, 1, security_id)
    struct.pack_into("<f", packet, 8, ltp)
    struct.pack_into("<h", packet, 12, 10)
    struct.pack_into("<i", packet, 14, ltt)
    struct.pack_into("<i", packet, 22, volume)
    struct.pack_into("<f", packet, 34, 980.0)
    struct.pack_into("<f", packet, 42, 990.0)
    struct.pack_into("<f", packet, 46, 975.0)
    return bytes(packet)


def _depth_packet(security_id: int = 3045, side_code: int = 41) -> bytes:
    packet = bytearray(332)
    struct.pack_into("<h", packet, 0, 332)
    packet[2] = side_code
    packet[3] = 1
    struct.pack_into("<i", packet, 4, security_id)
    for level in range(20):
        offset = 12 + level * 16
        struct.pack_into("<d", packet, offset, 987.0 - level * 0.05)
        struct.pack_into("<I", packet, offset + 8, 100 + level)
        struct.pack_into("<I", packet, offset + 12, 3 + level)
    return bytes(packet)


def test_documented_epoch_is_true_utc_instant_of_ist_session_start() -> None:
    timestamp = datetime.fromtimestamp(DOCUMENTED_IST_DAILY_EPOCH, timezone.utc)
    assert timestamp == datetime(2012, 1, 10, 18, 30, tzinfo=timezone.utc)
    assert timestamp.astimezone(IST).date().isoformat() == "2012-01-11"
    assert timestamp.astimezone(IST).hour == 0


def test_daily_candle_completion_waits_for_session_close() -> None:
    session_day = datetime(2024, 5, 10, 3, 45, tzinfo=timezone.utc)  # 09:15 IST
    before_close = datetime(2024, 5, 10, 9, 0, tzinfo=timezone.utc)  # 14:30 IST
    after_close = datetime(2024, 5, 10, 10, 1, tzinfo=timezone.utc)  # 15:31 IST
    assert not _dhan_candle_is_complete(session_day, "1d", before_close)
    assert _dhan_candle_is_complete(session_day, "1d", after_close)


def test_intraday_candle_completion_requires_interval_to_elapse() -> None:
    bar_start = datetime(2024, 5, 10, 4, 0, tzinfo=timezone.utc)
    assert not _dhan_candle_is_complete(bar_start, "5m", datetime(2024, 5, 10, 4, 4, tzinfo=timezone.utc))
    assert _dhan_candle_is_complete(bar_start, "5m", datetime(2024, 5, 10, 4, 5, tzinfo=timezone.utc))


def _historical_payload(timestamps: list[int], opens=None, highs=None, lows=None, closes=None, volumes=None):
    count = len(timestamps)
    return {
        "open": opens or [100.0] * count,
        "high": highs or [110.0] * count,
        "low": lows or [95.0] * count,
        "close": closes or [105.0] * count,
        "volume": volumes or [1000] * count,
        "timestamp": timestamps,
    }


def _fetch_daily(payload, start, end, now):
    captured = {}

    def fake_request(method, url, **kwargs):
        captured["body"] = kwargs["json"]
        captured["url"] = url
        return payload

    with patch("app.broker_adapters._request", fake_request), \
            patch("app.broker_adapters._wait_for_dhan_market_data", lambda: None), \
            patch("app.broker_adapters.datetime", wraps=datetime) as mocked_datetime:
        mocked_datetime.now.return_value = now
        bars = DhanAdapter().get_historical_bars(
            "token", {"client_id": "client"}, SBIN_SEGMENT, SBIN_SECURITY_ID, "1d", start, end
        )
    return bars, captured


def test_daily_request_uses_non_inclusive_to_date_and_correct_endpoint() -> None:
    start = datetime(2024, 5, 1, tzinfo=timezone.utc)
    end = datetime(2024, 5, 10, 12, 0, tzinfo=timezone.utc)
    now = datetime(2024, 5, 11, 12, 0, tzinfo=timezone.utc)
    _, captured = _fetch_daily(_historical_payload([]), start, end, now)
    assert captured["url"].endswith("/v2/charts/historical")
    assert captured["body"]["securityId"] == SBIN_SECURITY_ID
    assert captured["body"]["exchangeSegment"] == SBIN_SEGMENT
    assert captured["body"]["instrument"] == "EQUITY"
    assert captured["body"]["fromDate"] == "2024-05-01"
    assert captured["body"]["toDate"] == "2024-05-11"


def test_empty_historical_response_is_an_explicit_empty_result() -> None:
    start = datetime(2024, 5, 1, tzinfo=timezone.utc)
    end = datetime(2024, 5, 10, tzinfo=timezone.utc)
    bars, _ = _fetch_daily(_historical_payload([]), start, end, end)
    assert bars == []


def test_misaligned_historical_arrays_are_rejected() -> None:
    payload = _historical_payload([DOCUMENTED_IST_DAILY_EPOCH])
    payload["close"] = []
    with pytest.raises(BrokerAdapterError):
        _fetch_daily(
            payload,
            datetime(2012, 1, 1, tzinfo=timezone.utc),
            datetime(2012, 2, 1, tzinfo=timezone.utc),
            datetime(2012, 2, 1, tzinfo=timezone.utc),
        )


def test_invalid_ohlc_candle_is_rejected_rather_than_stored() -> None:
    payload = _historical_payload([DOCUMENTED_IST_DAILY_EPOCH], highs=[90.0])
    with pytest.raises(BrokerAdapterError):
        _fetch_daily(
            payload,
            datetime(2012, 1, 1, tzinfo=timezone.utc),
            datetime(2012, 2, 1, tzinfo=timezone.utc),
            datetime(2012, 2, 1, tzinfo=timezone.utc),
        )


def test_incomplete_current_session_candle_is_excluded() -> None:
    # Session for 2012-01-11 IST; "now" is 12:00 IST that day, before the 15:30 close.
    now = datetime(2012, 1, 11, 6, 30, tzinfo=timezone.utc)
    start = datetime(2012, 1, 1, tzinfo=timezone.utc)
    end = datetime(2012, 1, 20, tzinfo=timezone.utc)
    bars, _ = _fetch_daily(
        _historical_payload([DOCUMENTED_IST_DAILY_EPOCH]), start, end, now
    )
    assert bars == []


def test_ticker_packet_decodes_ltp_and_ist_timestamp() -> None:
    bar = DhanAdapter.decode_market_feed_packet(
        _ticker_packet(), SBIN_SEGMENT, SBIN_SECURITY_ID, price_tick=Decimal("0.05")
    )
    assert bar is not None
    assert bar.close == bar.open == bar.high == bar.low
    assert bar.close == Decimal("987.5")
    assert bar.timestamp == _dhan_ist_epoch_to_utc(1700000000)


def test_quote_packet_decodes_documented_offsets() -> None:
    bar = DhanAdapter.decode_market_feed_packet(
        _quote_packet(), SBIN_SEGMENT, SBIN_SECURITY_ID, price_tick=Decimal("0.05")
    )
    assert bar is not None
    assert bar.close == pytest.approx(987.5)
    assert bar.open == pytest.approx(980.0)
    assert bar.high == pytest.approx(990.0)
    assert bar.low == pytest.approx(975.0)
    assert bar.volume == 1234567
    assert bar.timestamp == _dhan_ist_epoch_to_utc(1700000000)


def test_feed_packet_for_other_instrument_is_rejected() -> None:
    with pytest.raises(BrokerAdapterError):
        DhanAdapter.decode_market_feed_packet(_quote_packet(security_id=9999), SBIN_SEGMENT, SBIN_SECURITY_ID)


def test_feed_packet_for_other_segment_is_rejected() -> None:
    with pytest.raises(BrokerAdapterError):
        DhanAdapter.decode_market_feed_packet(_ticker_packet(), "BSE_EQ", SBIN_SECURITY_ID)


def test_server_disconnect_packet_raises_feed_disconnected() -> None:
    packet = bytearray(10)
    struct.pack_into("<h", packet, 1, 10)
    packet[0] = 50
    with pytest.raises(BrokerFeedDisconnected):
        DhanAdapter.decode_market_feed_packet(bytes(packet), SBIN_SEGMENT, SBIN_SECURITY_ID)


def test_depth_packet_identity_comes_from_header_and_levels_decode() -> None:
    depth = DhanAdapter.decode_market_depth_packet(_depth_packet(), SBIN_SEGMENT, SBIN_SECURITY_ID)
    assert depth is not None
    assert depth["side"] == "bids"
    assert len(depth["levels"]) == 20
    assert depth["levels"][0]["quantity"] == 100
    assert depth["levels"][0]["orders"] == 3


def test_depth_packet_for_other_instrument_is_not_associated() -> None:
    with pytest.raises(BrokerAdapterError):
        DhanAdapter.decode_market_depth_packet(
            _depth_packet(security_id=1333), SBIN_SEGMENT, SBIN_SECURITY_ID
        )


def test_depth_ask_side_is_labelled_asks() -> None:
    depth = DhanAdapter.decode_market_depth_packet(
        _depth_packet(side_code=51), SBIN_SEGMENT, SBIN_SECURITY_ID
    )
    assert depth is not None and depth["side"] == "asks"


def test_v2_daily_epoch_maps_to_ist_session_midnight() -> None:
    # Documented sample: 2012-01-11 00:00 IST encoded as a UTC wall-clock epoch.
    adapter = DhanV2Adapter({"access_token": "token", "client_id": "client"})
    payload = {
        "open": [1], "high": [2], "low": [1], "close": [2], "volume": [10],
        "timestamp": [DOCUMENTED_IST_DAILY_EPOCH],
    }
    start = datetime(2012, 1, 9, tzinfo=timezone.utc)
    end = datetime(2012, 1, 12, tzinfo=timezone.utc)
    with patch.object(adapter, "_post_payload", return_value=payload) as post:
        bars = adapter.get_historical_bars(SBIN_SEGMENT, SBIN_SECURITY_ID, "1d", start, end)
    assert post.call_args.args[0] == "/charts/historical"
    assert bars[0].timestamp == datetime(2012, 1, 10, 18, 30, tzinfo=timezone.utc)
    assert bars[0].timestamp.astimezone(IST).date().isoformat() == "2012-01-11"


def test_v2_daily_request_uses_non_inclusive_to_date() -> None:
    adapter = DhanV2Adapter({"access_token": "token", "client_id": "client"})
    payload = {"open": [], "high": [], "low": [], "close": [], "volume": [], "timestamp": []}
    start = datetime(2024, 5, 1, tzinfo=timezone.utc)
    end = datetime(2024, 5, 10, 12, 0, tzinfo=timezone.utc)
    with patch.object(adapter, "_post_payload", return_value=payload) as post:
        adapter.get_historical_bars(SBIN_SEGMENT, SBIN_SECURITY_ID, "1d", start, end)
    body = post.call_args.args[1]
    assert body["securityId"] == SBIN_SECURITY_ID
    assert body["exchangeSegment"] == SBIN_SEGMENT
    assert body["fromDate"] == "2024-05-01"
    assert body["toDate"] == "2024-05-10"


def _label_candidate(timezone_name: str, country: str) -> InstrumentCandidate:
    return InstrumentCandidate(
        provider="dhan",
        provider_instrument_id="3045",
        provider_symbol="SBIN",
        symbol="SBIN",
        display_name="SBIN",
        asset_class="equity",
        venue="NSE",
        currency="INR" if country == "IN" else "USD",
        country=country,
        timezone=timezone_name,
        is_synthetic=False,
        is_verified=True,
    )


def _label_bar(timestamp: datetime) -> HistoricalBar:
    return HistoricalBar(
        timestamp=timestamp,
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100.5"),
        volume=Decimal("1000"),
    )


def test_documented_daily_epoch_has_ist_session_date_2012_01_11() -> None:
    raw = datetime.fromtimestamp(DOCUMENTED_IST_DAILY_EPOCH, timezone.utc)
    bar = _label_bar(raw)

    normalized = market_data._normalize_bar_timestamp(
        bar, _label_candidate("Asia/Kolkata", "IN"), "1d"
    )

    assert raw == datetime(2012, 1, 10, 18, 30, tzinfo=timezone.utc)
    assert normalized.timestamp == datetime(2012, 1, 11, tzinfo=timezone.utc)
    assert market_data._daily_session_date(normalized.timestamp) == date(2012, 1, 11)
    assert (normalized.open, normalized.high, normalized.low, normalized.close) == (
        bar.open, bar.high, bar.low, bar.close
    )


def test_us_daily_session_date_is_not_shifted_to_previous_day() -> None:
    stored_key = datetime(2026, 10, 30, tzinfo=timezone.utc)

    assert market_data._daily_session_date(stored_key) == date(2026, 10, 30)


def test_intraday_timestamps_are_unchanged_by_daily_normalization() -> None:
    raw = datetime(2012, 1, 10, 18, 30, tzinfo=timezone.utc)
    candidate = _label_candidate("Asia/Kolkata", "IN")

    for interval in ("1m", "5m", "15m", "1h"):
        normalized = market_data._normalize_bar_timestamp(
            _label_bar(raw), candidate, interval
        )
        assert normalized.timestamp == raw


def test_float32_price_keeps_no_artefact_and_is_quantised_to_tick() -> None:
    bar = DhanAdapter.decode_market_feed_packet(
        _ticker_packet(ltp=1420.7), SBIN_SEGMENT, SBIN_SECURITY_ID, price_tick=Decimal("0.05")
    )
    assert bar is not None
    assert bar.close == Decimal("1420.70")
    assert "0.0000" not in str(bar.close)


def test_price_near_one_lakh_forty_thousand_keeps_paise_through_float32_path() -> None:
    # MRF-scale price: the float32 ULP here is 1/64, so a quarter-rupee value is exact.
    bar = DhanAdapter.decode_market_feed_packet(
        _ticker_packet(ltp=140000.25), SBIN_SEGMENT, SBIN_SECURITY_ID, price_tick=Decimal("0.05")
    )
    assert bar is not None
    assert bar.close == Decimal("140000.25")


def test_price_packet_without_resolvable_tick_fails_closed() -> None:
    with pytest.raises(BrokerAdapterError, match="tick size"):
        DhanAdapter.decode_market_feed_packet(_ticker_packet(), SBIN_SEGMENT, SBIN_SECURITY_ID)
    with pytest.raises(BrokerAdapterError, match="tick size"):
        DhanAdapter.decode_market_feed_packet(_quote_packet(), SBIN_SEGMENT, SBIN_SECURITY_ID)


def test_tick_size_resolves_from_scrip_master_and_fails_closed_when_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    master = [
        {
            "EXCH_ID": "NSE",
            "SECURITY_ID": SBIN_SECURITY_ID,
            "INSTRUMENT": "EQUITY",
            "TICK_SIZE": "0.05",
        }
    ]
    monkeypatch.setattr(DhanAdapter, "_scrip_master", classmethod(lambda cls: master))
    monkeypatch.setattr("app.broker_adapters._DHAN_TICK_CACHE", {})
    assert DhanAdapter.price_tick_for(SBIN_SEGMENT, SBIN_SECURITY_ID) == Decimal("0.05")
    assert DhanAdapter.price_tick_for(SBIN_SEGMENT, "99999") is None
    assert DhanAdapter.price_tick_for("NSE_FNO", SBIN_SECURITY_ID) is None


def test_yahoo_float32_frame_bar_is_artefact_free_through_text_frame() -> None:
    import numpy as np
    import pandas as pd

    from app.provider_adapters import _HttpProvider, _provider_text_frame

    frame = pd.DataFrame(
        {
            "Open": np.array([1431.3], dtype=np.float32),
            "High": np.array([1431.3], dtype=np.float32),
            "Low": np.array([1431.3], dtype=np.float32),
            "Close": np.array([1431.3], dtype=np.float32),
            "Volume": np.array([1000], dtype=np.int64),
        },
        index=pd.DatetimeIndex([datetime(2026, 11, 2, 9, 15, tzinfo=timezone.utc)]),
    )
    text_frame = _provider_text_frame(frame)
    values = [text_frame.iat[0, column] for column in range(5)]
    assert all(isinstance(value, str) for value in values[:4])
    bar = _HttpProvider._bar(frame.index[0].to_pydatetime(), *values)
    assert bar.open == bar.high == bar.low == bar.close == Decimal("1431.30000000")
