"""Binance candle fixes until Hummingbot forwards the capped REST limit itself."""

import pandas as pd
from hummingbot.data_feed.candles_feed.binance_perpetual_candles.binance_perpetual_candles import BinancePerpetualCandles
from hummingbot.data_feed.candles_feed.binance_spot_candles.binance_spot_candles import BinanceSpotCandles
from hummingbot.data_feed.candles_feed.candles_factory import CandlesFactory


class _BoundedBinanceCandles:
    def _round_timestamp_to_interval_multiple(self, timestamp):
        # Weeks open on Monday; calendar months are not fixed 30-day periods.
        stamp = pd.Timestamp(timestamp, unit="s", tz="UTC")
        if self.interval == "1w":
            return int((stamp.normalize() - pd.Timedelta(days=stamp.weekday())).timestamp())
        if self.interval == "1M":
            return int(stamp.replace(day=1).normalize().timestamp())
        return super()._round_timestamp_to_interval_multiple(timestamp)

    def _advance_time(self, timestamp, count):
        if self.interval == "1M":
            return int((pd.Timestamp(timestamp, unit="s", tz="UTC") + pd.DateOffset(months=count)).timestamp())
        return timestamp + count * self.interval_in_seconds

    def _interval_count(self, start, end):
        if self.interval == "1M":
            first, last = pd.Timestamp(start, unit="s", tz="UTC"), pd.Timestamp(end, unit="s", tz="UTC")
            return (last.year - first.year) * 12 + last.month - first.month
        return int((end - start) / self.interval_in_seconds)

    async def fetch_candles(self, start_time=None, end_time=None, limit=None):
        if self.interval == "1M" and end_time is None and start_time is not None:
            count = self.candles_max_result_per_rest_request if limit is None else limit
            count = min(max(count, 1), self.candles_max_result_per_rest_request)
            end_time = self._advance_time(start_time, count)
        return await super().fetch_candles(start_time=start_time, end_time=end_time, limit=limit)

    def _get_rest_candles_params(self, start_time=None, end_time=None, limit=None):
        # CandlesBase already caps the window using the requested limit, but omits
        # that limit when calling this hook. Both Binance feeds have inclusive REST
        # boundaries with no extra interval offsets, so the window recovers it.
        if start_time is not None and end_time is not None:
            limit = max(1, int((end_time - start_time) / self.interval_in_seconds))
        limit = min(limit or self.candles_max_result_per_rest_request, self.candles_max_result_per_rest_request)
        if self.interval == "1M" and end_time is not None:
            # Recover the capped count from CandlesBase's fixed-second window,
            # then replace that window with the actual calendar-month range.
            start_time = self._advance_time(end_time, -limit)
        params = super()._get_rest_candles_params(start_time, end_time, limit)
        if start_time is not None:
            params["startTime"] = int(start_time * 1000)
        if end_time is not None:
            params["endTime"] = int(end_time * 1000) - 1
        return params

    async def get_historical_candles(self, config):
        # Binance includes both REST boundaries. Download half-open pages and add
        # one interval to the last page so the public API's inclusive end is kept.
        # Advancing by the requested window also terminates on gaps/empty pages.
        start = self._round_timestamp_to_interval_multiple(config.start_time)
        cursor = self._advance_time(self._round_timestamp_to_interval_multiple(config.end_time), 1)
        rows = []
        while cursor > start:
            count = min(self.candles_max_result_per_rest_request, max(1, self._interval_count(start, cursor)))
            candles = await self.fetch_candles(end_time=cursor, limit=count)
            rows.extend(candles.tolist())
            cursor = self._advance_time(cursor, -count)
        frame = pd.DataFrame(rows, columns=self.columns)
        frame = frame.drop_duplicates(subset=["timestamp"]).sort_values("timestamp")
        return frame.loc[(frame["timestamp"] >= config.start_time) & (frame["timestamp"] <= config.end_time)].reset_index(
            drop=True
        )


class APIBinanceSpotCandles(_BoundedBinanceCandles, BinanceSpotCandles):
    pass


class APIBinancePerpetualCandles(_BoundedBinanceCandles, BinancePerpetualCandles):
    @property
    def candles_max_result_per_rest_request(self):
        # 499 rows cost 2 weight; 500 costs 5, and 1500 costs 10. Pagination and
        # live backfills already continue until the requested window is covered.
        return 499


def create_candle_feed(config):
    cls = {"binance": APIBinanceSpotCandles, "binance_perpetual": APIBinancePerpetualCandles}.get(config.connector)
    if cls is None:
        return CandlesFactory.get_candle(config)
    return cls(trading_pair=config.trading_pair, interval=config.interval, max_records=config.max_records)
