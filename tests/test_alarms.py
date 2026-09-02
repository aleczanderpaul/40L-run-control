'''Unit tests for core_tools/alarms.py -- pure Python, no Qt, no display required.
Run with: pytest tests/test_alarms.py -v'''

import math

import pandas as pd
import pytest

from core_tools.alarms import AlarmEvaluator, AlarmSpec, AlarmState, DisplayStatus, display_status
from core_tools.gui.get_data_for_GUI import get_outer_vessel_gauge_pressure


def feed(evaluator, channel_id, spec, values, start_t=0.0, dt=1.0, log_interval_s=1.0):
    '''Feed `values` as consecutive samples 1 tick apart (each its own evaluate_channel
    call, matching how a real scan only sees new rows since the previous scan), and
    return the state after the last one.'''
    t = start_t
    for value in values:
        evaluator.evaluate_channel(channel_id, spec, [(t, value)], now=t, log_interval_s=log_interval_s)
        t += dt
    return evaluator.state_for(channel_id)


# --- boundary: must be strictly greater to trip ---

def test_boundary_below_limit_never_trips():
    spec = AlarmSpec(high=40.0, clear_high=38.0)
    evaluator = AlarmEvaluator()
    state = feed(evaluator, 'vmm', spec, [39.9, 39.9, 39.9])
    assert state.state == AlarmState.OK


def test_boundary_exactly_at_limit_does_not_trip():
    spec = AlarmSpec(high=40.0, clear_high=38.0)
    evaluator = AlarmEvaluator()
    state = feed(evaluator, 'vmm', spec, [40.0, 40.0, 40.0])
    assert state.state == AlarmState.OK, "exactly at the limit must not trip -- comparison is strictly greater"


def test_boundary_just_above_limit_trips():
    spec = AlarmSpec(high=40.0, clear_high=38.0)
    evaluator = AlarmEvaluator()
    state = feed(evaluator, 'vmm', spec, [40.1, 40.1, 40.1])
    assert state.state == AlarmState.ALARM


# --- debounce ---

def test_debounce_two_consecutive_breaches_does_not_trip():
    spec = AlarmSpec(high=40.0, clear_high=38.0, consecutive_samples=3)
    evaluator = AlarmEvaluator()
    state = feed(evaluator, 'vmm', spec, [41.0, 41.0])
    assert state.state == AlarmState.OK


def test_debounce_three_consecutive_breaches_trips():
    spec = AlarmSpec(high=40.0, clear_high=38.0, consecutive_samples=3)
    evaluator = AlarmEvaluator()
    state = feed(evaluator, 'vmm', spec, [41.0, 41.0, 41.0])
    assert state.state == AlarmState.ALARM


def test_debounce_resets_on_in_range_sample():
    spec = AlarmSpec(high=40.0, clear_high=38.0, consecutive_samples=3)
    evaluator = AlarmEvaluator()
    state = feed(evaluator, 'vmm', spec, [41.0, 41.0, 39.0, 41.0, 41.0])
    assert state.state == AlarmState.OK, "an in-range sample must reset the debounce counter"


# --- deadband ---

def test_deadband_does_not_clear_inside_the_band():
    spec = AlarmSpec(high=40.0, clear_high=38.0, consecutive_samples=3)
    evaluator = AlarmEvaluator()
    state = feed(evaluator, 'vmm', spec, [41.0, 41.0, 41.0])
    assert state.state == AlarmState.ALARM
    evaluator.evaluate_channel('vmm', spec, [(10.0, 39.0)], now=10.0, log_interval_s=1.0)
    assert evaluator.state_for('vmm').state == AlarmState.ALARM, "39 is inside the deadband (limit=40, clear=38) -- must stay ALARM"


def test_deadband_clears_at_the_clear_threshold():
    spec = AlarmSpec(high=40.0, clear_high=38.0, consecutive_samples=3)
    evaluator = AlarmEvaluator()
    feed(evaluator, 'vmm', spec, [41.0, 41.0, 41.0])
    evaluator.evaluate_channel('vmm', spec, [(10.0, 38.0)], now=10.0, log_interval_s=1.0)
    assert evaluator.state_for('vmm').state == AlarmState.OK


# --- gauge pressure |p| > 5.0 (strictly greater), independent of sign ---

def test_gauge_pressure_exactly_five_does_not_trip():
    spec = AlarmSpec(abs_high=5.0, clear_abs_high=4.5)
    evaluator = AlarmEvaluator()
    state = feed(evaluator, 'gauge', spec, [5.0, 5.0, 5.0])
    assert state.state == AlarmState.OK, "a saturated +-5V sensor reads exactly 5.00 and must not trip"


def test_gauge_pressure_above_five_trips():
    spec = AlarmSpec(abs_high=5.0, clear_abs_high=4.5)
    evaluator = AlarmEvaluator()
    state = feed(evaluator, 'gauge', spec, [5.01, 5.01, 5.01])
    assert state.state == AlarmState.ALARM


def test_gauge_pressure_negative_above_five_trips():
    spec = AlarmSpec(abs_high=5.0, clear_abs_high=4.5)
    evaluator = AlarmEvaluator()
    state = feed(evaluator, 'gauge', spec, [-5.01, -5.01, -5.01])
    assert state.state == AlarmState.ALARM


# --- outer vessel: real Pascal->Torr conversion, and Units=Off -> NO_DATA ---

def test_outer_vessel_pascal_conversion_trips_alarm():
    # 760 Torr / 0.0075006168 Torr-per-Pascal ~= 101324 Pa; use a comfortably higher value.
    df = pd.DataFrame({'Gauge 1': [110000.0, 110000.0, 110000.0], 'Units': ['Pascal', 'Pascal', 'Pascal']})
    pressures = get_outer_vessel_gauge_pressure(df, 1)
    assert all(p > 760.0 for p in pressures), "sanity check on the conversion itself"

    spec = AlarmSpec(high=760.0, clear_high=750.0)
    evaluator = AlarmEvaluator()
    state = feed(evaluator, 'ov_g1', spec, list(pressures))
    assert state.state == AlarmState.ALARM


def test_outer_vessel_units_off_yields_no_data_not_ok():
    df = pd.DataFrame({'Gauge 1': [13.93], 'Units': ['Off']})
    pressures = get_outer_vessel_gauge_pressure(df, 1)
    assert math.isnan(pressures.iloc[0])

    spec = AlarmSpec(high=760.0, clear_high=750.0)
    evaluator = AlarmEvaluator()
    state = feed(evaluator, 'ov_g1', spec, list(pressures))
    assert state.state == AlarmState.NO_DATA


# --- NaN never satisfies an OK comparison ---

def test_nan_is_no_data_not_ok_even_with_no_limit_configured():
    evaluator = AlarmEvaluator()
    state = feed(evaluator, 'x', None, [float('nan')])
    assert state.state == AlarmState.NO_DATA


def test_nan_does_not_trip_and_does_not_read_as_ok():
    spec = AlarmSpec(high=40.0, clear_high=38.0)
    evaluator = AlarmEvaluator()
    state = feed(evaluator, 'x', spec, [float('nan'), float('nan'), float('nan')])
    assert state.state == AlarmState.NO_DATA
    assert state.state != AlarmState.OK
    assert state.state != AlarmState.ALARM


# --- staleness ---

def test_staleness_fires_at_five_times_interval_and_clears_on_fresh_sample():
    spec = AlarmSpec(high=40.0, clear_high=38.0, stale_multiplier=5)
    evaluator = AlarmEvaluator()
    log_interval_s = 2.0

    evaluator.evaluate_channel('vmm', spec, [(0.0, 30.0)], now=0.0, log_interval_s=log_interval_s)
    assert evaluator.state_for('vmm').state == AlarmState.OK

    # just under the threshold (5 * 2s = 10s) -- must not be stale yet
    evaluator.evaluate_channel('vmm', spec, [], now=9.9, log_interval_s=log_interval_s)
    assert evaluator.state_for('vmm').state == AlarmState.OK

    # past the threshold
    evaluator.evaluate_channel('vmm', spec, [], now=10.1, log_interval_s=log_interval_s)
    assert evaluator.state_for('vmm').state == AlarmState.STALE

    # a fresh sample clears it immediately, no debounce needed
    evaluator.evaluate_channel('vmm', spec, [(10.2, 30.0)], now=10.2, log_interval_s=log_interval_s)
    assert evaluator.state_for('vmm').state == AlarmState.OK


def test_staleness_applies_even_without_an_alarm_spec():
    evaluator = AlarmEvaluator()
    evaluator.evaluate_channel('flow', None, [(0.0, 1.0)], now=0.0, log_interval_s=1.0)
    evaluator.evaluate_channel('flow', None, [], now=100.0, log_interval_s=1.0)
    assert evaluator.state_for('flow').state == AlarmState.STALE


# --- latching + acknowledge ---

def test_latching_full_lifecycle():
    spec = AlarmSpec(high=40.0, clear_high=38.0, consecutive_samples=3)
    evaluator = AlarmEvaluator()

    feed(evaluator, 'vmm', spec, [41.0, 41.0, 41.0])
    state = evaluator.state_for('vmm')
    assert state.state == AlarmState.ALARM
    assert display_status(state) == DisplayStatus.ALARM

    # value returns in range -> "cleared, unacknowledged", not silently OK
    evaluator.evaluate_channel('vmm', spec, [(10.0, 30.0)], now=10.0, log_interval_s=1.0)
    state = evaluator.state_for('vmm')
    assert state.state == AlarmState.OK
    assert state.latched is True
    assert state.acknowledged is False
    assert display_status(state) == DisplayStatus.CLEARED

    # Acknowledge -> fully OK
    evaluator.acknowledge('vmm', now=11.0)
    state = evaluator.state_for('vmm')
    assert state.latched is False
    assert display_status(state) == DisplayStatus.OK

    # re-trigger after clearing raises the banner again (fresh debounce required)
    evaluator.evaluate_channel('vmm', spec, [(12.0, 41.0), (13.0, 41.0)], now=13.0, log_interval_s=1.0)
    assert evaluator.state_for('vmm').state == AlarmState.OK, "only 2 fresh breaching samples -- must not trip yet"
    evaluator.evaluate_channel('vmm', spec, [(14.0, 41.0)], now=14.0, log_interval_s=1.0)
    state = evaluator.state_for('vmm')
    assert state.state == AlarmState.ALARM
    assert state.acknowledged is False
    assert display_status(state) == DisplayStatus.ALARM


def test_acknowledge_while_still_active_dismisses_banner_but_stays_alarm():
    spec = AlarmSpec(high=40.0, clear_high=38.0, consecutive_samples=3)
    evaluator = AlarmEvaluator()
    feed(evaluator, 'vmm', spec, [41.0, 41.0, 41.0])

    evaluator.acknowledge('vmm', now=10.0)
    state = evaluator.state_for('vmm')
    assert state.state == AlarmState.ALARM, "acknowledging a still-active alarm must not change the live condition"
    assert state.acknowledged is True

    # it clears afterward -- this re-raises the banner even though it was acked while active
    evaluator.evaluate_channel('vmm', spec, [(11.0, 30.0)], now=11.0, log_interval_s=1.0)
    state = evaluator.state_for('vmm')
    assert state.state == AlarmState.OK
    assert state.latched is True
    assert state.acknowledged is False
    assert display_status(state) == DisplayStatus.CLEARED


# --- alarms evaluate independently of plot pause state ---

def test_evaluator_has_no_concept_of_pause_and_keeps_evaluating():
    # The evaluator's API takes no "paused" argument anywhere -- pausing a plot is a
    # GUI-only concern (whether curve.setData() gets called), so there is nothing a
    # paused plot could do to suppress this. Demonstrated by evaluating normally with
    # no plot/GUI object involved at all.
    spec = AlarmSpec(high=40.0, clear_high=38.0, consecutive_samples=3)
    evaluator = AlarmEvaluator()
    state = feed(evaluator, 'vmm', spec, [41.0, 41.0, 41.0])
    assert state.state == AlarmState.ALARM


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v']))
