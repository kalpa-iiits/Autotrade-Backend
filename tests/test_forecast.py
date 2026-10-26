from datetime import datetime, timezone

import pytest

from app.forecast.service import Forecast, ForecastBar, ForecastUnavailable, future_times
from tests.conftest import AUTH


def ts(*args) -> int:
    return int(datetime(*args, tzinfo=timezone.utc).timestamp())


def at(t: int) -> datetime:
    return datetime.fromtimestamp(t, timezone.utc).replace(tzinfo=None)


def test_intraday_bars_continue_next_session():
    # Friday 15:15 (last 15m bar) -> Monday 09:15 onwards.
    out = [at(t) for t in future_times(ts(2026, 10, 23, 15, 15), "15m", 3)]
    assert out == [datetime(2026, 10, 26, 9, 15), datetime(2026, 10, 26, 9, 30), datetime(2026, 10, 26, 9, 45)]


def test_hourly_and_daily_and_weekly_steps():
    assert [at(t) for t in future_times(ts(2026, 10, 26, 14, 15), "1h", 2)] == [
        datetime(2026, 10, 26, 15, 15), datetime(2026, 10, 27, 9, 15)]
    assert [at(t) for t in future_times(ts(2026, 10, 22), "1D", 3)] == [
        datetime(2026, 10, 23), datetime(2026, 10, 26), datetime(2026, 10, 27)]
    assert [at(t) for t in future_times(ts(2026, 10, 19), "1W", 2)] == [datetime(2026, 10, 26), datetime(2026, 11, 2)]


def bars(n=40, step=300):
    start = ts(2026, 10, 26, 9, 15)
    return [{"time": start + i * step, "open": 100 + i, "high": 101 + i, "low": 99 + i, "close": 100.5 + i, "volume": 1000}
            for i in range(n)]


class FakeForecaster:
    model_name = "fake/kronos"

    def __init__(self, error=None):
        self.error = error
        self.calls = []

    async def forecast(self, bars, timeframe, horizon, samples):
        if self.error:
            raise self.error
        self.calls.append((len(bars), timeframe, horizon, samples))
        times = future_times(bars[-1]["time"], timeframe, horizon)
        return Forecast([ForecastBar(t, 1, 2, 0.5, 1.5, 10, 1.2, 1.8) for t in times],
                        [[1.2] * horizon, [1.8] * horizon])


@pytest.fixture
def fake(client):
    f = FakeForecaster()
    client.app.state.forecaster = f
    return f


def test_forecast(client, fake):
    r = client.post("/api/forecast", json={"bars": bars(), "timeframe": "5m", "horizon": 5}, headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["model"] == "fake/kronos" and len(body["points"]) == 5
    assert body["points"][0]["time"] == bars()[-1]["time"] + 300
    assert body["runs"] == [[1.2] * 5, [1.8] * 5]
    assert fake.calls == [(40, "5m", 5, 30)]


def test_forecast_validates(client, fake):
    assert client.post("/api/forecast", json={"bars": bars(10), "timeframe": "5m"}, headers=AUTH).status_code == 422
    assert client.post("/api/forecast", json={"bars": bars(), "timeframe": "2m"}, headers=AUTH).status_code == 422
    assert client.post("/api/forecast", json={"bars": bars(), "timeframe": "5m", "horizon": 500}, headers=AUTH).status_code == 422
    unordered = bars()
    unordered[5]["time"] = unordered[4]["time"]
    assert client.post("/api/forecast", json={"bars": unordered, "timeframe": "5m"}, headers=AUTH).status_code == 422
    assert fake.calls == []


def test_forecast_needs_login(client, fake):
    assert client.post("/api/forecast", json={"bars": bars(), "timeframe": "5m"}).status_code == 401


def test_forecast_unavailable(client):
    client.app.state.forecaster = FakeForecaster(ForecastUnavailable("Forecasts need extra packages"))
    r = client.post("/api/forecast", json={"bars": bars(), "timeframe": "5m"}, headers=AUTH)
    assert r.status_code == 503 and "extra packages" in r.json()["detail"]
