"""Running Kronos: one model per process, loaded on the first forecast, one forecast at a time.

Times here are "IST wall clock as epoch seconds": the seconds a UTC clock would show if it read
IST (what the Market chart plots). Kronos reads minute / hour / weekday / day / month from them,
so they must be exchange-local, and future bars must fall in NSE sessions."""

import asyncio
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.core.config import Settings

log = logging.getLogger(__name__)

SESSION_OPEN = timedelta(hours=9, minutes=15)
SESSION_CLOSE = timedelta(hours=15, minutes=30)
# Seconds per bar for intraday timeframes.
INTRADAY = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600}


class ForecastUnavailable(Exception):
    """Kronos can't run here: its packages aren't installed or the model didn't load."""


def _next_weekday(day: datetime) -> datetime:
    day += timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def future_times(last: int, timeframe: str, count: int) -> list[int]:
    """Start times of the `count` bars after the one starting at `last`, in NSE sessions
    (09:15 to 15:30, Monday to Friday; exchange holidays are not known here)."""
    t = datetime.fromtimestamp(last, timezone.utc)
    out = []
    for _ in range(count):
        if timeframe == "1W":
            t += timedelta(days=7)
        elif timeframe == "1D":
            t = _next_weekday(t)
        else:
            t += timedelta(seconds=INTRADAY[timeframe])
            midnight = t.replace(hour=0, minute=0, second=0, microsecond=0)
            if t - midnight >= SESSION_CLOSE:
                t = _next_weekday(midnight) + SESSION_OPEN
        out.append(int(t.timestamp()))
    return out


@dataclass
class ForecastBar:
    time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    # Spread of the sampled paths' closes: 10th and 90th percentile.
    close_p10: float
    close_p90: float


@dataclass
class Forecast:
    bars: list[ForecastBar]  # the mean of the runs
    runs: list[list[float]]  # each run's closes, bar by bar


class Forecaster:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._predictor = None
        self._load_lock = threading.Lock()
        self._run_lock = asyncio.Lock()  # one forecast at a time: each uses all CPU cores

    @property
    def model_name(self) -> str:
        return self.settings.forecast_model

    def _load(self):
        with self._load_lock:
            if self._predictor is None:
                try:
                    from app.forecast.kronos import Kronos, KronosPredictor, KronosTokenizer
                except ImportError as e:
                    raise ForecastUnavailable(
                        f"Forecasts need extra packages ({e.name} is missing): "
                        "pip install -r requirements-forecast.txt") from e
                s = self.settings
                log.info("Loading forecast model %s (tokenizer %s)", s.forecast_model, s.forecast_tokenizer)
                try:
                    tokenizer = KronosTokenizer.from_pretrained(s.forecast_tokenizer)
                    model = Kronos.from_pretrained(s.forecast_model)
                except Exception as e:
                    raise ForecastUnavailable(f"Couldn't load {s.forecast_model}: {e}") from e
                tokenizer.eval()
                model.eval()
                self._predictor = KronosPredictor(model, tokenizer, device=s.forecast_device or None,
                                                  max_context=s.forecast_max_context)
            return self._predictor

    def _run(self, bars: list[dict], timeframe: str, horizon: int, samples: int) -> Forecast:
        import numpy as np
        import pandas as pd
        import torch

        predictor = self._load()
        bars = bars[-self.settings.forecast_max_context:]
        df = pd.DataFrame(bars, columns=["time", "open", "high", "low", "close", "volume"])
        x_time = pd.Series(pd.to_datetime(df["time"], unit="s"))
        times = future_times(bars[-1]["time"], timeframe, horizon)
        y_time = pd.Series(pd.to_datetime(times, unit="s"))
        x = df[["open", "high", "low", "close", "volume"]].astype("float64")

        # Each sample is one sampled path (the predictor would average them away); the same
        # bars give the same forecast, so the chart doesn't jump when it's asked again.
        torch.manual_seed(bars[-1]["time"] ^ len(bars))
        paths = predictor.predict_batch(
            [x] * samples, [x_time] * samples, [y_time] * samples, pred_len=horizon,
            T=1.0, top_p=0.9, sample_count=1, verbose=False)
        stacked = np.stack([p[["open", "high", "low", "close", "volume"]].to_numpy() for p in paths])
        mean = stacked.mean(axis=0)
        p10, p90 = np.percentile(stacked[:, :, 3], [10, 90], axis=0)

        out = []
        for i, t in enumerate(times):
            o, h, l, c, v = (float(n) for n in mean[i])
            out.append(ForecastBar(time=t, open=o, high=max(o, h, c), low=min(o, l, c), close=c,
                                   volume=max(0.0, v), close_p10=float(p10[i]), close_p90=float(p90[i])))
        return Forecast(out, stacked[:, :, 3].round(4).tolist())

    async def forecast(self, bars: list[dict], timeframe: str, horizon: int, samples: int) -> Forecast:
        async with self._run_lock:
            return await asyncio.to_thread(self._run, bars, timeframe, horizon, samples)
