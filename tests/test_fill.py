'''The two-stage gas fill's decisions: which rate at which pressure, and when to refuse.

Importing live_plotter_GUI_class pulls in PyQt5, but only as an import -- these are
plain arithmetic over the numbers in the fill's boxes and no QApplication (and so no
display) is created, so this suite runs headless. Same arrangement as test_row_counts.py.

This is the only part of the program that opens gas from a measurement rather than from
an operator's keystroke, so the cases below are mostly about refusing to.
'''

import pytest

from core_tools.gui.live_plotter_GUI_class import (
    fill_stage_for, fill_flow_for, fill_settings_error)

FAST, SLOW = 10.0, 1.0
SLOW_AT, TARGET = 700.0, 740.0


class TestStage:
    def test_well_below_the_handover_is_fast(self):
        assert fill_stage_for(500.0, SLOW_AT, TARGET) == 'fast'

    def test_between_handover_and_target_is_slow(self):
        assert fill_stage_for(720.0, SLOW_AT, TARGET) == 'slow'

    # Inclusive on purpose: stopping a hair early is harmless, since the vessel coasts
    # up after the valve shuts. Still filling AT the target is not.
    def test_exactly_at_the_target_is_done(self):
        assert fill_stage_for(TARGET, SLOW_AT, TARGET) == 'done'

    def test_exactly_at_the_handover_is_slow(self):
        assert fill_stage_for(SLOW_AT, SLOW_AT, TARGET) == 'slow'

    # A coarse logging interval can put the next reading past both thresholds at once.
    # Target is checked first so that finishes the fill rather than merely slowing it.
    def test_a_reading_past_both_thresholds_finishes_rather_than_slows(self):
        assert fill_stage_for(900.0, SLOW_AT, TARGET) == 'done'


class TestFlow:
    def test_each_stage_asks_for_its_own_rate(self):
        assert fill_flow_for('fast', FAST, SLOW) == FAST
        assert fill_flow_for('slow', FAST, SLOW) == SLOW
        assert fill_flow_for('done', FAST, SLOW) == 0.0
        assert fill_flow_for('aborted', FAST, SLOW) == 0.0

    # Pressure dithering across the handover would otherwise swing the MFC between
    # rates, and a fill whose pressure is FALLING means a leak or a pump -- neither of
    # which is a reason to open the valve wider.
    def test_flow_never_goes_back_up(self):
        assert fill_flow_for('fast', FAST, SLOW, previous_flow=SLOW) == SLOW

    def test_the_ratchet_cannot_reopen_a_closed_valve(self):
        assert fill_flow_for('fast', FAST, SLOW, previous_flow=0.0) == 0.0

    def test_lowering_is_always_allowed(self):
        assert fill_flow_for('slow', FAST, SLOW, previous_flow=FAST) == SLOW


class TestSettings:
    def ok(self, **kw):
        args = dict(fast_flow=FAST, slow_flow=SLOW, slow_at=SLOW_AT,
                    target=TARGET, pressure=500.0, alarm_limit=760.0)
        args.update(kw)
        return fill_settings_error(**args)

    def test_sensible_settings_are_accepted(self):
        assert self.ok() is None

    # Filling blind is the one thing this must never do, including at the very start.
    def test_no_pressure_reading_is_refused(self):
        assert 'no usable pressure' in self.ok(pressure=None)

    def test_handover_at_or_above_the_target_is_refused(self):
        assert 'must be below the target' in self.ok(slow_at=740.0)

    def test_slow_rate_faster_than_the_fast_rate_is_refused(self):
        assert 'must not exceed' in self.ok(slow_flow=20.0)

    # A zero slow rate would stall forever at the handover with the valve shut.
    def test_zero_slow_rate_is_refused(self):
        assert 'above zero' in self.ok(slow_flow=0.0)

    def test_already_at_the_target_is_refused(self):
        assert 'already at or above' in self.ok(pressure=TARGET)

    # Filling to or past the over-pressure alarm would trip the interlock at the top of
    # the fill, shutting the gas and latching the setpoint control. Refuse up front.
    def test_target_at_the_over_pressure_alarm_is_refused(self):
        assert 'trip the interlock' in self.ok(target=760.0)

    def test_target_above_the_over_pressure_alarm_is_refused(self):
        assert 'trip the interlock' in self.ok(target=800.0, slow_at=750.0)

    def test_a_channel_with_no_alarm_limit_imposes_no_ceiling(self):
        assert self.ok(target=800.0, slow_at=750.0, alarm_limit=None) is None
