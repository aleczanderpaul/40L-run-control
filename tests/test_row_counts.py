'''How many rows a scan reads versus how many a plot draws.

Importing live_plotter_GUI_class pulls in PyQt5, but only as an import -- these
functions are plain arithmetic and no QApplication (and so no display) is created,
so this suite still runs headless.

The regression these guard: one row count used to serve both purposes, so
ALARM_LOOKBACK_ROWS -- a floor that exists for alarm evaluation -- also inflated
what plots drew. A 1m window on a 2s channel drew 50 rows (~100s) instead of 30.
'''

import pandas as pd
import pytest

from core_tools.gui.live_plotter_GUI_class import (ALARM_LOOKBACK_ROWS, WINDOW_OPTIONS,
                                                   rows_for_window, rows_to_fetch, tail)

INTERVALS = [1, 2, 10, 60]


# --- the reported bug ---

def test_one_minute_on_a_two_second_channel_draws_only_its_window():
    assert rows_for_window(60, 2) == 30    # 60s / 2s -- not 50
    assert rows_to_fetch(60, 2) == 50      # the read still satisfies the alarm floor


def test_a_window_needing_fewer_rows_than_the_floor_is_not_inflated():
    '''The floor is the only reason these two ever differ, and it must not reach the
    plot: at 2s the drawn span stays 60s even though 50 rows are read.'''
    assert rows_for_window(60, 2) * 2 == 60
    assert rows_to_fetch(60, 2) * 2 == 100  # what the plot used to show


def test_one_minute_on_a_one_second_channel_was_never_affected():
    '''60 rows already clears the floor, which is why only 2s channels overshot.'''
    assert rows_for_window(60, 1) == rows_to_fetch(60, 1) == 60


# --- the invariant between them ---

@pytest.mark.parametrize('window_s', [seconds for _label, seconds in WINDOW_OPTIONS])
@pytest.mark.parametrize('interval', INTERVALS)
def test_the_read_always_covers_what_is_drawn(window_s, interval):
    '''Reading fewer rows than a plot draws would leave gaps; the read must be at
    least the window, always.'''
    assert rows_to_fetch(window_s, interval) >= rows_for_window(window_s, interval)


@pytest.mark.parametrize('window_s', [seconds for _label, seconds in WINDOW_OPTIONS])
@pytest.mark.parametrize('interval', INTERVALS)
def test_drawn_rows_cover_the_window_and_no_more(window_s, interval):
    rows = rows_for_window(window_s, interval)
    assert rows * interval >= window_s              # enough to fill it
    if rows > 2:
        # ...and not a whole sample more. Skipped where the two-point minimum binds
        # (a 1m window on a 60s channel), which overshoots by design -- see
        # test_a_window_shorter_than_one_sample_still_draws_a_line.
        assert (rows - 1) * interval < window_s


@pytest.mark.parametrize('window_s', [seconds for _label, seconds in WINDOW_OPTIONS])
@pytest.mark.parametrize('interval', INTERVALS)
def test_the_read_always_satisfies_the_alarm_floor(window_s, interval):
    '''The floor is why the two counts exist; reading below it would shorten alarm
    evaluation's history at short windows.'''
    assert rows_to_fetch(window_s, interval) >= ALARM_LOOKBACK_ROWS


def test_both_counts_grow_with_the_window():
    windows = [seconds for _label, seconds in WINDOW_OPTIONS]
    for interval in INTERVALS:
        drawn = [rows_for_window(w, interval) for w in windows]
        read = [rows_to_fetch(w, interval) for w in windows]
        assert drawn == sorted(drawn), (interval, drawn)
        assert read == sorted(read), (interval, read)


def test_a_window_shorter_than_one_sample_still_draws_a_line():
    '''Two points minimum -- one point isn't a plot.'''
    assert rows_for_window(60, 600) == 2


# --- tail(), which does the trimming ---

def test_tail_takes_the_last_rows_of_a_series():
    series = pd.Series([0, 1, 2, 3, 4, 5])
    assert list(tail(series, 2)) == [4, 5]


def test_tail_is_positional_on_a_series_whose_index_is_not_positional():
    '''The data path hands over Series sliced off the end of a DataFrame, so their
    index starts wherever that slice began -- tail() must count from the end, not
    read those integers as labels.'''
    series = pd.Series([10, 20, 30, 40], index=[100, 101, 102, 103])
    assert list(tail(series, 2)) == [30, 40]


def test_tail_works_on_a_plain_list_too():
    assert list(tail([1, 2, 3, 4], 3)) == [2, 3, 4]


def test_tail_asking_for_more_than_there_is_returns_everything():
    assert list(tail(pd.Series([1, 2]), 30)) == [1, 2]
