"""Binance candle fixes until Hummingbot forwards the capped REST limit itself."""

import pandas as pd
from hummingbot.data_feed.candles_feed.binance_perpetual_candles.binance_perpetual_candles import BinancePerpetualCandles
from hummingbot.data_feed.candles_feed.binance_spot_candles.binance_spot_candles import BinanceSpotCandles
from hummingbot.data_feed.candles_feed.candles_factory import CandlesFactory


class _BoundedBinanceCandles:
    def _get_rest_candles_params(self, start_time=None, end_time=None, limit=None):
        # CandlesBase already caps the window using the requested limit, but omits
        # that limit when calling this hook. Both Binance feeds have inclusive REST
        # boundaries with no extra interval offsets, so the window recovers it.
        if start_time is not None and end_time is not None:
            limit = max(1, int((end_time - start_time) / self.interval_in_seconds))
        limit = min(limit or self.candles_max_result_per_rest_request, self.candles_max_result_per_rest_request)
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
        step = self.interval_in_seconds
        start = self._round_timestamp_to_interval_multiple(config.start_time)
        cursor = self._round_timestamp_to_interval_multiple(config.end_time) + step
        rows = []
        while cursor > start:
            count = min(self.candles_max_result_per_rest_request, max(1, int((cursor - start) / step)))
            candles = await self.fetch_candles(end_time=cursor, limit=count)
            rows.extend(candles.tolist())
            cursor -= count * step
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
