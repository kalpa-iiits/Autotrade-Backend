from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.auth.dependencies import require_user
from app.forecast.service import ForecastUnavailable

router = APIRouter(prefix="/forecast", tags=["forecast"], dependencies=[Depends(require_user)])

Timeframe = Literal["1m", "5m", "15m", "1h", "1D", "1W"]


class Bar(BaseModel):
    time: int = Field(description="Bar start, IST wall clock as epoch seconds (as the chart plots it)")
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


class ForecastRequest(BaseModel):
    bars: list[Bar] = Field(min_length=32, max_length=2048, description="Oldest first")
    timeframe: Timeframe
    horizon: int = Field(24, ge=1, le=120, description="Bars to predict")
    samples: int = Field(30, ge=1, le=32, description="Sampled runs, averaged; their spread gives the band")


class ForecastPoint(BaseModel):
    time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_p10: float
    close_p90: float


class ForecastResponse(BaseModel):
    model: str
    timeframe: Timeframe
    points: list[ForecastPoint]
    runs: list[list[float]] = Field(description="Each sampled run's closes, one per point")


@router.post("", response_model=ForecastResponse)
async def forecast(body: ForecastRequest, request: Request):
    """The next `horizon` bars after `bars`, predicted by Kronos: the mean of `samples` sampled
    runs, the 10th-90th percentile of their closes, and every run's closes. Future bars fall in
    NSE sessions. A forecast, not advice: the first one also downloads the model (a few seconds to a minute).
    503 when forecasting isn't installed on this server."""
    bars = [b.model_dump() for b in body.bars]
    if any(b["time"] >= n["time"] for b, n in zip(bars, bars[1:])):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "bars must be oldest first, one per time")
    forecaster = request.app.state.forecaster
    try:
        result = await forecaster.forecast(bars, body.timeframe, body.horizon, body.samples)
    except ForecastUnavailable as e:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(e))
    return {"model": forecaster.model_name, "timeframe": body.timeframe,
            "points": [p.__dict__ for p in result.bars], "runs": result.runs}
