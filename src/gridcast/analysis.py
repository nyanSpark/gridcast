"""Forecast-skill metrics and derived weather features.

Kept as pure functions over arrays so they are unit-testable and reusable
outside the SQL path (notebooks, the export summary, tests).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

CELSIUS_TO_FAHRENHEIT_OFFSET = 32.0
DEGREE_DAY_BASE_F = 65.0


def to_fahrenheit(celsius: pd.Series | np.ndarray) -> np.ndarray:
    return np.asarray(celsius, dtype="float64") * 9.0 / 5.0 + CELSIUS_TO_FAHRENHEIT_OFFSET


def mae(predicted: pd.Series, actual: pd.Series) -> float:
    """Mean absolute error."""
    diff = (pd.Series(predicted) - pd.Series(actual)).dropna()
    return float(diff.abs().mean()) if len(diff) else float("nan")


def bias(predicted: pd.Series, actual: pd.Series) -> float:
    """Signed mean error. Positive means the forecast ran high."""
    diff = (pd.Series(predicted) - pd.Series(actual)).dropna()
    return float(diff.mean()) if len(diff) else float("nan")


def mape(predicted: pd.Series, actual: pd.Series) -> float:
    """Mean absolute percentage error, in percent.

    Zero actuals are excluded rather than producing infinities -- for load data
    a zero is a data gap, not a real reading.
    """
    frame = pd.DataFrame({"p": pd.Series(predicted), "a": pd.Series(actual)}).dropna()
    frame = frame[frame["a"] != 0]
    if frame.empty:
        return float("nan")
    return float(((frame["p"] - frame["a"]).abs() / frame["a"]).mean() * 100.0)


def skill_score(predicted: pd.Series, actual: pd.Series, reference: pd.Series) -> float:
    """1 - MAE(forecast) / MAE(reference). Positive means it beats the baseline.

    The usual reference is persistence (yesterday's value at the same hour). A
    forecast that cannot beat persistence is not adding information, which is
    the honest way to read an accuracy chart.
    """
    forecast_error = mae(predicted, actual)
    reference_error = mae(reference, actual)
    if not reference_error or np.isnan(reference_error):
        return float("nan")
    return float(1.0 - forecast_error / reference_error)


def degree_hours(temperature_c: pd.Series, base_f: float = DEGREE_DAY_BASE_F) -> pd.DataFrame:
    """Cooling and heating degree hours -- the feature that actually predicts load.

    Raw temperature is a poor regressor because the load response is V-shaped:
    demand rises both above and below the comfort band. Splitting into CDH/HDH
    linearises each side, which is why utility load models are built on these
    rather than on temperature directly.
    """
    fahrenheit = to_fahrenheit(temperature_c)
    return pd.DataFrame(
        {
            "cooling_degree_hours": np.clip(fahrenheit - base_f, 0, None),
            "heating_degree_hours": np.clip(base_f - fahrenheit, 0, None),
        }
    )


def persistence_baseline(series: pd.Series, periods: int) -> pd.Series:
    """Shift by one day's worth of periods to build a persistence forecast."""
    return pd.Series(series).shift(periods)
