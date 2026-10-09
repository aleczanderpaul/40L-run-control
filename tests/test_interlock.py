'''When a safety interlock may trip, and when it may arm or be reset.

Importing live_plotter_GUI_class pulls in PyQt5, but only as an import -- these two
functions are pure decisions over alarm state and no QApplication (and so no display)
is created, so this suite runs headless. Same arrangement as test_row_counts.py.

Two regressions these guard, both of which make an interlock worse than useless:

1. ChannelAlarmState starts at OK before anything has been read. Treating that
   default as a good reading arms the interlock on a channel nobody has heard from --
   and it then trips the moment the scan notices the log file is stale, which is
   every launch made before the logger is started. An interlock that cries wolf on
   every startup is one operators learn to ignore.
2. NO_DATA must not trip. core_tools/alarms.py reaches it on NaN readings and treats
   an intentionally-off gauge as normal, so tripping on it would shut the gas every
   time a gauge is deliberately switched off.
'''

import pytest

from core_tools.alarms import AlarmState, AlarmTransition, ChannelAlarmState
from core_tools.gui.models import Interlock
from core_tools.gui.live_plotter_GUI_class import (interlock_should_trip, interlock_reset_blockers,
                                                   interlock_auto_reset_blockers, interlock_auto_reset_due,
                                                   logger_restart_delay, LOGGER_FAST_RESTARTS,
                                                   LOGGER_RESTART_DELAY_S, LOGGER_SLOW_RESTART_DELAY_S)

TRIGGERS = ['ov_pressure_g1', 'ov_pressure_g2']


def transition(channel_id, to_state, from_state=AlarmState.OK):
    return AlarmTransition(channel_id=channel_id, from_state=from_state, to_state=to_state,
                            timestamp=1000.0, value=765.0, message=f'{channel_id}: 765.0 > 760.0')


def state(alarm_state=AlarmState.OK, last_timestamp=1000.0, consecutive_breach=0):
    return ChannelAlarmState(channel_id='c', state=alarm_state, last_timestamp=last_timestamp,
                             consecutive_breach=consecutive_breach)


class TestShouldTrip:
    @pytest.mark.parametrize('channel_id', TRIGGERS)
    def test_alarm_on_any_trigger_channel_trips(self, channel_id):
        assert interlock_should_trip(transition(channel_id, AlarmState.ALARM), TRIGGERS, trip_on_stale=True)

    def test_alarm_trips_even_when_stale_tripping_is_off(self):
        assert interlock_should_trip(transition(TRIGGERS[0], AlarmState.ALARM), TRIGGERS, trip_on_stale=False)

    def test_stale_trips_only_when_configured(self):
        stale = transition(TRIGGERS[0], AlarmState.STALE)
        assert interlock_should_trip(stale, TRIGGERS, trip_on_stale=True)
        assert not interlock_should_trip(stale, TRIGGERS, trip_on_stale=False)

    # An intentionally-off gauge is normal (see core_tools/alarms.py); shutting the
    # gas every time one is switched off would be a nuisance trip, not a safety one.
    def test_no_data_never_trips(self):
        no_data = transition(TRIGGERS[0], AlarmState.NO_DATA)
        assert not interlock_should_trip(no_data, TRIGGERS, trip_on_stale=True)
        assert not interlock_should_trip(no_data, TRIGGERS, trip_on_stale=False)

    # Recovery transitions (ALARM -> OK, "data resumed") must not re-fire it.
    def test_returning_to_ok_never_trips(self):
        recovered = transition(TRIGGERS[0], AlarmState.OK, from_state=AlarmState.ALARM)
        assert not interlock_should_trip(recovered, TRIGGERS, trip_on_stale=True)

    def test_untriggered_channel_is_ignored(self):
        assert not interlock_should_trip(transition('filter_line_flow', AlarmState.ALARM),
                                          TRIGGERS, trip_on_stale=True)


class TestResetBlockers:
    def test_all_ok_is_unblocked(self):
        assert interlock_reset_blockers({c: state() for c in TRIGGERS}) == []

    # The startup case: nothing read yet. The default state is OK, so only the
    # missing timestamp distinguishes "confirmed good" from "never heard from".
    def test_channel_with_no_data_yet_blocks(self):
        blockers = interlock_reset_blockers({
            'ov_pressure_g1': state(last_timestamp=None),
            'ov_pressure_g2': state(),
        })
        assert blockers == [('ov_pressure_g1', 'no data yet')]

    def test_no_data_at_all_blocks_every_channel(self):
        blockers = interlock_reset_blockers({c: state(last_timestamp=None) for c in TRIGGERS})
        assert [c for c, _ in blockers] == TRIGGERS

    # Not merely "not in ALARM": readings that have stopped arriving block a reset
    # just as a high reading does, because gas must not be reopened on a pressure
    # nobody can see.
    @pytest.mark.parametrize('alarm_state', [AlarmState.ALARM, AlarmState.STALE])
    def test_alarm_and_stale_block(self, alarm_state):
        blockers = interlock_reset_blockers({'ov_pressure_g1': state(alarm_state), 'ov_pressure_g2': state()})
        assert blockers == [('ov_pressure_g1', alarm_state.value)]

    # The 40L's low-range OV gauge is switched off above ~1 Torr, which reads as
    # NO_DATA. Counting that as a blocker meant the interlock could never arm during
    # normal operation and would not have tripped at 765 Torr -- the safety feature
    # silently inert exactly when it was needed. A deliberately-off gauge is not a
    # fault, which is why the trip rule ignores NO_DATA too.
    def test_switched_off_gauge_does_not_block_while_another_reads(self):
        assert interlock_reset_blockers({
            'ov_pressure_g1': state(),                       # high-range gauge, reading
            'ov_pressure_g2': state(AlarmState.NO_DATA),     # low-range gauge, switched off
        }) == []

    # ...but arming with no usable gauge at all must never happen.
    def test_all_channels_off_blocks(self):
        blockers = interlock_reset_blockers({c: state(AlarmState.NO_DATA) for c in TRIGGERS})
        assert [c for c, _ in blockers] == TRIGGERS

    # An off gauge alongside one that is stale is still no usable reading.
    def test_off_gauge_plus_stale_gauge_blocks_both(self):
        blockers = interlock_reset_blockers({
            'ov_pressure_g1': state(AlarmState.STALE),
            'ov_pressure_g2': state(AlarmState.NO_DATA),
        })
        assert blockers == [('ov_pressure_g1', 'STALE'), ('ov_pressure_g2', 'NO_DATA')]

    def test_reports_every_blocking_channel(self):
        blockers = interlock_reset_blockers({
            'ov_pressure_g1': state(AlarmState.ALARM),
            'ov_pressure_g2': state(AlarmState.STALE),
        })
        assert blockers == [('ov_pressure_g1', 'ALARM'), ('ov_pressure_g2', 'STALE')]


class TestAutoResetBlockers:
    def test_good_readings_are_unblocked(self):
        assert interlock_auto_reset_blockers({c: state() for c in TRIGGERS}) == []

    # Coming back from STALE restarts the alarm debounce, so a vessel that comes back
    # over-pressure reads OK for a couple of samples before it reads ALARM. A manual
    # reset has a person looking at the number; an automatic one must not unlock on it.
    def test_reading_over_limit_but_not_yet_alarm_blocks(self):
        blockers = interlock_auto_reset_blockers({
            'ov_pressure_g1': state(consecutive_breach=1),
            'ov_pressure_g2': state(),
        })
        assert blockers == [('ov_pressure_g1', 'over limit')]

    def test_includes_everything_that_blocks_a_manual_reset(self):
        blockers = interlock_auto_reset_blockers({
            'ov_pressure_g1': state(AlarmState.STALE),
            'ov_pressure_g2': state(AlarmState.NO_DATA),
        })
        assert blockers == [('ov_pressure_g1', 'STALE'), ('ov_pressure_g2', 'NO_DATA')]


def tripped(cause='stale', confirmed=True, auto=True):
    return Interlock(id='i', label='I', trigger_channel_ids=TRIGGERS, setpoint_control_id='sp',
                     safe_value=0.0, auto_reset_after_stale=auto,
                     tripped=True, trip_cause=cause, command_confirmed=confirmed)


class TestAutoResetDue:
    HOLD = 6.0

    def test_stale_trip_resets_after_readings_stay_good_for_the_hold(self):
        interlock = tripped()
        assert not interlock_auto_reset_due(interlock, [], 100.0, self.HOLD)
        assert not interlock_auto_reset_due(interlock, [], 105.0, self.HOLD)
        assert interlock_auto_reset_due(interlock, [], 106.0, self.HOLD)

    # The point of the whole feature: an over-pressure always needs a person.
    def test_alarm_trip_never_resets_itself(self):
        interlock = tripped(cause='alarm')
        assert not interlock_auto_reset_due(interlock, [], 100.0, self.HOLD)
        assert not interlock_auto_reset_due(interlock, [], 1000.0, self.HOLD)

    def test_not_declared_auto_reset_never_resets_itself(self):
        interlock = tripped(auto=False)
        assert not interlock_auto_reset_due(interlock, [], 100.0, self.HOLD)
        assert not interlock_auto_reset_due(interlock, [], 1000.0, self.HOLD)

    # Unconfirmed means the gas may still be flowing; resetting would also clear the
    # SHUT THE GAS MANUALLY warning after the retries give up.
    def test_unconfirmed_safe_command_blocks(self):
        interlock = tripped(confirmed=False)
        assert not interlock_auto_reset_due(interlock, [], 100.0, self.HOLD)
        assert not interlock_auto_reset_due(interlock, [], 1000.0, self.HOLD)

    # A blip in the middle of the hold restarts it -- "good for 6s" means unbroken.
    def test_any_blocker_restarts_the_hold(self):
        interlock = tripped()
        interlock_auto_reset_due(interlock, [], 100.0, self.HOLD)
        assert not interlock_auto_reset_due(interlock, [('ov_pressure_g1', 'STALE')], 104.0, self.HOLD)
        assert interlock.good_since is None
        assert not interlock_auto_reset_due(interlock, [], 105.0, self.HOLD)
        assert not interlock_auto_reset_due(interlock, [], 110.0, self.HOLD)
        assert interlock_auto_reset_due(interlock, [], 111.0, self.HOLD)

    def test_untripped_interlock_is_left_alone(self):
        interlock = tripped()
        interlock.tripped = False
        assert not interlock_auto_reset_due(interlock, [], 1000.0, self.HOLD)


class TestLoggerRestartDelay:
    def test_fast_restarts_then_backs_off_but_never_stops(self):
        assert logger_restart_delay(1) == LOGGER_RESTART_DELAY_S
        assert logger_restart_delay(LOGGER_FAST_RESTARTS) == LOGGER_RESTART_DELAY_S
        assert logger_restart_delay(LOGGER_FAST_RESTARTS + 1) == LOGGER_SLOW_RESTART_DELAY_S
        assert logger_restart_delay(1000) == LOGGER_SLOW_RESTART_DELAY_S

    # Must restart well inside the 10s it takes a 2s channel to go STALE, or a single
    # bad reply from the console still trips the interlock.
    def test_first_restart_beats_staleness_at_2s_logging(self):
        assert logger_restart_delay(1) + 2.0 < 5 * 2.0
