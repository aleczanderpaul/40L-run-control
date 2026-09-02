import numpy as np
import pytest

from core_tools.gui.decimate import decimate_min_max


def test_below_cap_returns_unchanged():
    x = np.arange(100)
    y = np.sin(x)
    out_x, out_y = decimate_min_max(x, y, max_points=20000)
    assert list(out_x) == list(x)
    assert list(out_y) == list(y)


def test_decimates_above_cap():
    x = np.arange(50000)
    y = np.zeros(50000)
    out_x, out_y = decimate_min_max(x, y, max_points=20000)
    assert len(out_x) <= 20000
    assert len(out_x) == len(out_y)


def test_output_is_time_ordered():
    x = np.arange(50000)
    y = np.sin(x / 100.0)
    out_x, out_y = decimate_min_max(x, y, max_points=20000)
    assert list(out_x) == sorted(out_x)


def test_spike_is_never_hidden_unlike_naive_stride():
    n = 100000
    x = np.arange(n)
    y = np.zeros(n)
    spike_index = 12345
    y[spike_index] = 1000.0  # a single-sample spike, easy for a naive stride to skip

    max_points = 2000
    stride = n // max_points
    strided_y = y[::stride]
    assert 1000.0 not in strided_y, "sanity check: naive stride does drop this spike"

    out_x, out_y = decimate_min_max(x, y, max_points=max_points)
    assert 1000.0 in out_y, "min/max decimation must preserve the spike naive striding would drop"


def test_handles_nan_values_without_crashing():
    x = np.arange(50000)
    y = np.full(50000, np.nan)
    y[100] = 5.0
    out_x, out_y = decimate_min_max(x, y, max_points=20000)
    assert len(out_x) <= 20000


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v']))
