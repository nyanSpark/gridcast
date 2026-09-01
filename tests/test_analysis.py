from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from gridcast.analysis import bias, degree_hours, mae, mape, skill_score, to_fahrenheit


def test_mae_and_bias_separate_magnitude_from_direction():
    predicted = pd.Series([10.0, 20.0, 30.0])
    actual = pd.Series([12.0, 18.0, 30.0])
    assert mae(predicted, actual) == pytest.approx(4 / 3)
    assert bias(predicted, actual) == pytest.approx(0.0)  # errors cancel


def test_mape_is_a_percentage():
    assert mape(pd.Series([110.0]), pd.Series([100.0])) == pytest.approx(10.0)


def test_mape_ignores_zero_actuals_instead_of_returning_infinity():
    predicted = pd.Series([110.0, 5.0])
    actual = pd.Series([100.0, 0.0])
    assert mape(predicted, actual) == pytest.approx(10.0)


def test_metrics_drop_missing_pairs():
    predicted = pd.Series([10.0, np.nan, 30.0])
    actual = pd.Series([12.0, 18.0, np.nan])
    assert mae(predicted, actual) == pytest.approx(2.0)


def test_metrics_on_empty_input_are_nan_not_an_exception():
    assert np.isnan(mae(pd.Series(dtype=float), pd.Series(dtype=float)))
    assert np.isnan(mape(pd.Series(dtype=float), pd.Series(dtype=float)))


def test_skill_score_is_positive_when_the_forecast_beats_the_baseline():
    actual = pd.Series([10.0, 12.0, 14.0, 16.0])
    good = pd.Series([10.5, 12.5, 14.5, 16.5])     # off by 0.5
    baseline = pd.Series([12.0, 14.0, 16.0, 18.0])  # off by 2.0
    assert skill_score(good, actual, baseline) == pytest.approx(0.75)


def test_skill_score_is_negative_when_it_loses_to_the_baseline():
    actual = pd.Series([10.0, 12.0])
    poor = pd.Series([20.0, 22.0])
    baseline = pd.Series([11.0, 13.0])
    assert skill_score(poor, actual, baseline) < 0


def test_fahrenheit_conversion():
    assert to_fahrenheit(pd.Series([0.0, 100.0, 37.0])) == pytest.approx([32.0, 212.0, 98.6])


def test_degree_hours_split_the_v_shaped_response_into_two_linear_arms():
    # 65F base: 30C == 86F -> 21 cooling; 0C == 32F -> 33 heating.
    result = degree_hours(pd.Series([30.0, 0.0, 18.333333]))
    assert result["cooling_degree_hours"].tolist() == pytest.approx([21.0, 0.0, 0.0], abs=1e-3)
    assert result["heating_degree_hours"].tolist() == pytest.approx([0.0, 33.0, 0.0], abs=1e-3)


def test_degree_hours_are_never_negative():
    result = degree_hours(pd.Series([-40.0, 50.0]))
    assert (result >= 0).all().all()
