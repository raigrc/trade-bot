"""Tests for tradebot.alpha package."""

from __future__ import annotations

from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

from tradebot.alpha import AlphaSnapshot
from tradebot.alpha.composite import CompositeAlpha
from tradebot.alpha.funding_rate import FundingRateProvider
from tradebot.alpha.news_sentiment import NewsSentimentProvider
from tradebot.alpha.open_interest import OpenInterestProvider
from tradebot.alpha.sentiment import SentimentProvider


# ---------------------------------------------------------------------------
# AlphaSnapshot
# ---------------------------------------------------------------------------


class TestAlphaSnapshot:
    def test_defaults(self) -> None:
        snap = AlphaSnapshot()
        assert snap.funding_rate is None
        assert snap.open_interest_delta is None
        assert snap.sentiment_score is None
        assert snap.news_sentiment is None
        assert snap.timestamp_ms == 0

    def test_all_fields(self) -> None:
        snap = AlphaSnapshot(
            funding_rate=0.0001,
            open_interest_delta=2.5,
            sentiment_score=72.0,
            news_sentiment=-0.3,
            timestamp_ms=1_700_000_000_000,
        )
        assert snap.funding_rate == pytest.approx(0.0001)
        assert snap.open_interest_delta == pytest.approx(2.5)
        assert snap.sentiment_score == pytest.approx(72.0)
        assert snap.news_sentiment == pytest.approx(-0.3)
        assert snap.timestamp_ms == 1_700_000_000_000

    def test_frozen(self) -> None:
        snap = AlphaSnapshot(funding_rate=0.001)
        with pytest.raises(AttributeError):
            snap.funding_rate = 0.002  # type: ignore[misc]


# ---------------------------------------------------------------------------
# FundingRateProvider
# ---------------------------------------------------------------------------


class TestFundingRateProvider:
    def test_fetch_returns_snapshot(self) -> None:
        provider = FundingRateProvider()
        mock_rate = {"fundingRate": 0.0003}
        with patch.object(provider._ex, "fetch_funding_rate", return_value=mock_rate):
            snap = provider.fetch("BTC/USDT", now_ms=1_000_000)
            assert snap is not None
            assert snap.funding_rate == pytest.approx(0.0003)

    def test_fetch_returns_none_on_error(self) -> None:
        provider = FundingRateProvider()
        with patch.object(
            provider._ex,
            "fetch_funding_rate",
            side_effect=Exception("network down"),
        ):
            snap = provider.fetch("BTC/USDT", now_ms=1_000_000)
            assert snap is None

    def test_cache_hit_within_ttl(self) -> None:
        provider = FundingRateProvider()
        mock_rate = {"fundingRate": 0.0005}
        with patch.object(provider._ex, "fetch_funding_rate", return_value=mock_rate) as mock:
            snap1 = provider.fetch("BTC/USDT", now_ms=1_000_000)
            snap2 = provider.fetch("BTC/USDT", now_ms=1_000_000 + 1000)
            assert snap1.funding_rate == snap2.funding_rate
            mock.assert_called_once()  # only one actual API call

    def test_cache_miss_after_ttl(self) -> None:
        provider = FundingRateProvider()
        mock_rate = {"fundingRate": 0.0005}
        ttl_ms = 4 * 60 * 60 * 1000  # 4 hours
        with patch.object(provider._ex, "fetch_funding_rate", return_value=mock_rate) as mock:
            provider.fetch("BTC/USDT", now_ms=1_000_000)
            provider.fetch("BTC/USDT", now_ms=1_000_000 + ttl_ms + 1)
            assert mock.call_count == 2


# ---------------------------------------------------------------------------
# OpenInterestProvider
# ---------------------------------------------------------------------------


class TestOpenInterestProvider:
    def test_fetch_first_call_returns_zero_delta(self) -> None:
        provider = OpenInterestProvider()
        mock_oi = {"openInterestAmount": 50000.0}
        with patch.object(provider._ex, "fetch_open_interest", return_value=mock_oi):
            snap = provider.fetch("BTC/USDT", now_ms=1_000_000)
            assert snap is not None
            assert snap.open_interest_delta == pytest.approx(0.0)

    def test_fetch_returns_none_on_error(self) -> None:
        provider = OpenInterestProvider()
        with patch.object(
            provider._ex,
            "fetch_open_interest",
            side_effect=Exception("timeout"),
        ):
            snap = provider.fetch("BTC/USDT", now_ms=1_000_000)
            assert snap is None

    def test_cache_hit_within_ttl(self) -> None:
        provider = OpenInterestProvider()
        mock_oi = {"openInterestAmount": 50000.0}
        with patch.object(provider._ex, "fetch_open_interest", return_value=mock_oi) as mock:
            provider.fetch("BTC/USDT", now_ms=1_000_000)
            provider.fetch("BTC/USDT", now_ms=1_000_000 + 1000)
            mock.assert_called_once()

    def test_delta_computed_after_24h(self) -> None:
        provider = OpenInterestProvider()
        # First call establishes baseline
        with patch.object(
            provider._ex,
            "fetch_open_interest",
            return_value={"openInterestAmount": 100000.0},
        ):
            provider.fetch("BTC/USDT", now_ms=1_000_000)

        # Second call 24h later with different OI
        with patch.object(
            provider._ex,
            "fetch_open_interest",
            return_value={"openInterestAmount": 120000.0},
        ):
            snap = provider.fetch("BTC/USDT", now_ms=1_000_000 + 86_400_000)
            assert snap is not None
            assert snap.open_interest_delta == pytest.approx(20.0)  # (120k-100k)/100k * 100


# ---------------------------------------------------------------------------
# SentimentProvider
# ---------------------------------------------------------------------------


class TestSentimentProvider:
    def test_fetch_returns_score(self) -> None:
        provider = SentimentProvider()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"data": [{"value": "65", "value_classification": "Greed"}]}
        mock_resp.raise_for_status = MagicMock()
        with patch.object(provider._client, "get", return_value=mock_resp) as mock:
            snap = provider.fetch("BTC/USDT", now_ms=1_000_000)
            assert snap is not None
            assert snap.sentiment_score == pytest.approx(65.0)
            mock.assert_called_once()

    def test_fetch_returns_none_for_non_btc(self) -> None:
        provider = SentimentProvider()
        snap = provider.fetch("ETH/USDT", now_ms=1_000_000)
        assert snap is None

    def test_fetch_returns_none_on_error(self) -> None:
        provider = SentimentProvider()
        with patch.object(
            provider._client,
            "get",
            side_effect=Exception("connection refused"),
        ):
            snap = provider.fetch("BTC/USDT", now_ms=1_000_000)
            assert snap is None

    def test_cache_hit_within_ttl(self) -> None:
        provider = SentimentProvider()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"data": [{"value": "42"}]}
        mock_resp.raise_for_status = MagicMock()
        with patch.object(provider._client, "get", return_value=mock_resp) as mock:
            provider.fetch("BTC/USDT", now_ms=1_000_000)
            provider.fetch("BTC/USDT", now_ms=1_000_000 + 1000)
            mock.assert_called_once()


# ---------------------------------------------------------------------------
# NewsSentimentProvider
# ---------------------------------------------------------------------------


class TestNewsSentimentProvider:
    def test_fetch_returns_score(self) -> None:
        provider = NewsSentimentProvider()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"sentiment": 0.75}
        mock_resp.raise_for_status = MagicMock()
        with patch.object(provider._client, "get", return_value=mock_resp) as mock:
            snap = provider.fetch("BTC/USDT", now_ms=1_000_000)
            assert snap is not None
            assert snap.news_sentiment == pytest.approx(0.75)
            mock.assert_called_once_with(
                "https://cryptocurrency.cv/api/ai/sentiment",
                params={"asset": "BTC"},
            )

    def test_fetch_extracts_asset_from_symbol(self) -> None:
        provider = NewsSentimentProvider()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"sentiment": -0.2}
        mock_resp.raise_for_status = MagicMock()
        with patch.object(provider._client, "get", return_value=mock_resp) as mock:
            snap = provider.fetch("ETH/USDT", now_ms=1_000_000)
            assert snap is not None
            mock.assert_called_once_with(
                "https://cryptocurrency.cv/api/ai/sentiment",
                params={"asset": "ETH"},
            )

    def test_fetch_returns_none_on_error(self) -> None:
        provider = NewsSentimentProvider()
        with patch.object(
            provider._client,
            "get",
            side_effect=Exception("connection refused"),
        ):
            snap = provider.fetch("BTC/USDT", now_ms=1_000_000)
            assert snap is None

    def test_cache_hit_within_ttl(self) -> None:
        provider = NewsSentimentProvider()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"sentiment": 0.4}
        mock_resp.raise_for_status = MagicMock()
        with patch.object(provider._client, "get", return_value=mock_resp) as mock:
            provider.fetch("BTC/USDT", now_ms=1_000_000)
            provider.fetch("BTC/USDT", now_ms=1_000_000 + 1000)
            mock.assert_called_once()

    def test_cache_miss_after_ttl(self) -> None:
        provider = NewsSentimentProvider()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"sentiment": 0.4}
        mock_resp.raise_for_status = MagicMock()
        ttl_ms = 30 * 60 * 1000  # 30 minutes
        with patch.object(provider._client, "get", return_value=mock_resp) as mock:
            provider.fetch("BTC/USDT", now_ms=1_000_000)
            provider.fetch("BTC/USDT", now_ms=1_000_000 + ttl_ms + 1)
            assert mock.call_count == 2

    def test_default_zero_when_missing_field(self) -> None:
        provider = NewsSentimentProvider()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {}
        mock_resp.raise_for_status = MagicMock()
        with patch.object(provider._client, "get", return_value=mock_resp):
            snap = provider.fetch("BTC/USDT", now_ms=1_000_000)
            assert snap is not None
            assert snap.news_sentiment == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# CompositeAlpha
# ---------------------------------------------------------------------------


class TestCompositeAlpha:
    def _mock_provider(self, snap: Optional[AlphaSnapshot]) -> MagicMock:
        p = MagicMock()
        p.fetch.return_value = snap
        return p

    def test_merges_all_providers(self) -> None:
        funding = self._mock_provider(AlphaSnapshot(funding_rate=0.001, timestamp_ms=100))
        oi = self._mock_provider(AlphaSnapshot(open_interest_delta=5.0, timestamp_ms=200))
        sent = self._mock_provider(AlphaSnapshot(sentiment_score=72.0, timestamp_ms=300))
        news = self._mock_provider(AlphaSnapshot(news_sentiment=-0.3, timestamp_ms=400))

        alpha = CompositeAlpha(
            funding=funding, open_interest=oi, sentiment=sent, news_sentiment=news
        )
        snap = alpha.fetch("BTC/USDT", now_ms=500)

        assert snap.funding_rate == pytest.approx(0.001)
        assert snap.open_interest_delta == pytest.approx(5.0)
        assert snap.sentiment_score == pytest.approx(72.0)
        assert snap.news_sentiment == pytest.approx(-0.3)
        # Uses first available timestamp (from funding)
        assert snap.timestamp_ms == 100

    def test_partial_results_when_one_provider_fails(self) -> None:
        funding = self._mock_provider(AlphaSnapshot(funding_rate=-0.0005, timestamp_ms=100))
        oi = self._mock_provider(None)  # failed
        sent = self._mock_provider(AlphaSnapshot(sentiment_score=30.0, timestamp_ms=300))
        news = self._mock_provider(None)  # also failed

        alpha = CompositeAlpha(
            funding=funding, open_interest=oi, sentiment=sent, news_sentiment=news
        )
        snap = alpha.fetch("BTC/USDT", now_ms=500)

        assert snap.funding_rate == pytest.approx(-0.0005)
        assert snap.open_interest_delta is None
        assert snap.sentiment_score == pytest.approx(30.0)
        assert snap.news_sentiment is None

    def test_all_providers_fail(self) -> None:
        funding = self._mock_provider(None)
        oi = self._mock_provider(None)
        sent = self._mock_provider(None)
        news = self._mock_provider(None)

        alpha = CompositeAlpha(
            funding=funding, open_interest=oi, sentiment=sent, news_sentiment=news
        )
        snap = alpha.fetch("BTC/USDT", now_ms=500)

        assert snap.funding_rate is None
        assert snap.open_interest_delta is None
        assert snap.sentiment_score is None
        assert snap.news_sentiment is None
        assert snap.timestamp_ms == 500

    def test_unexpected_error_in_provider_is_caught(self) -> None:
        """The _safe_fetch wrapper catches unexpected exceptions."""
        funding = MagicMock()
        funding.fetch.side_effect = RuntimeError("kaboom")
        oi = self._mock_provider(None)
        sent = self._mock_provider(None)
        news = self._mock_provider(None)

        alpha = CompositeAlpha(
            funding=funding, open_interest=oi, sentiment=sent, news_sentiment=news
        )
        snap = alpha.fetch("BTC/USDT", now_ms=500)

        assert snap.funding_rate is None
        assert snap.open_interest_delta is None
        assert snap.sentiment_score is None
        assert snap.news_sentiment is None
