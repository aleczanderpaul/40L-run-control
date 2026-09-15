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
from core_tools.gui.live_plotter_GUI_class import interlock_should_trip, interlock_reset_blockers

TRIGGERS = ['ov_pressure_g1', 'ov_pressure_g2']


def transition(channel_id, to_state, from_state=AlarmState.OK):
    return AlarmTransition(channel_id=channel_id, from_state=from_state, to_state=to_state,
                            timestamp=1000.0, value=765.0, message=f'{channel_id}: 765.0 > 760.0')


def state(alarm_state=AlarmState.OK, last_timestamp=1000.0):
    return ChannelAlarmState(channel_id='c', state=alarm_state, last_timestamp=last_timestamp)


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

    # Not merely "not in ALARM": an unknown pressure blocks a reset just as a high
    # one does, because gas must not be reopened on a reading nobody can see.
    @pytest.mark.parametrize('alarm_state', [AlarmState.ALARM, AlarmState.STALE, AlarmState.NO_DATA])
    def test_any_non_ok_state_blocks(self, alarm_state):
        blockers = interlock_reset_blockers({'ov_pressure_g1': state(alarm_state), 'ov_pressure_g2': state()})
        assert blockers == [('ov_pressure_g1', alarm_state.value)]

    def test_reports_every_blocking_channel(self):
        blockers = interlock_reset_blockers({
            'ov_pressure_g1': state(AlarmState.ALARM),
            'ov_pressure_g2': state(AlarmState.STALE),
        })
        assert blockers == [('ov_pressure_g1', 'ALARM'), ('ov_pressure_g2', 'STALE')]
