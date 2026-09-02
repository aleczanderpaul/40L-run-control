from dataclasses import dataclass, field
from enum import Enum
import math

'''Pure-Python alarm state machine. No Qt imports here (and none should be added) so
this module can be evaluated headlessly and unit-tested without a display -- the GUI
subscribes to the AlarmTransition objects this produces, it never re-implements the
logic itself.

There is no warn tier -- AlarmState below is the complete, final set of raw
conditions. "CLEARED" is not a raw condition; it is a *presentation* of state==OK (or
NO_DATA) while an earlier ALARM/STALE episode is still latched and unacknowledged --
see DisplayStatus and display_status().'''


class AlarmState(Enum):
    OK = 'OK'
    ALARM = 'ALARM'
    STALE = 'STALE'
    NO_DATA = 'NO_DATA'


class DisplayStatus(Enum):
    OK = 'OK'
    ALARM = 'ALARM'
    STALE = 'STALE'
    CLEARED = 'CLEARED'
    NO_DATA = 'NO_DATA'


@dataclass
class AlarmSpec:
    high: float | None = None
    low: float | None = None
    clear_high: float | None = None
    clear_low: float | None = None
    abs_high: float | None = None
    clear_abs_high: float | None = None
    consecutive_samples: int = 3
    stale_multiplier: float = 5


@dataclass
class AlarmTransition:
    channel_id: str
    from_state: AlarmState
    to_state: AlarmState
    timestamp: float
    value: float | None
    message: str


@dataclass
class ChannelAlarmState:
    channel_id: str
    state: AlarmState = AlarmState.OK
    consecutive_breach: int = 0
    latched: bool = False       # True from the moment ALARM/STALE trips until Acknowledge fully clears it
    acknowledged: bool = True   # False while a latched episode awaits acknowledge
    peak_value: float | None = None
    peak_timestamp: float | None = None
    trip_timestamp: float | None = None
    last_value: float | None = None
    last_timestamp: float | None = None  # newest sample timestamp seen, of any value -- drives staleness


def display_status(state: ChannelAlarmState) -> DisplayStatus:
    '''What the GUI should actually render for this channel right now.'''
    if state.state == AlarmState.ALARM:
        return DisplayStatus.ALARM
    if state.state == AlarmState.STALE:
        return DisplayStatus.STALE
    if state.state == AlarmState.NO_DATA:
        return DisplayStatus.NO_DATA
    if state.latched and not state.acknowledged:
        return DisplayStatus.CLEARED
    return DisplayStatus.OK


def _breach(value, spec: AlarmSpec) -> bool:
    if spec.high is not None and value > spec.high:
        return True
    if spec.low is not None and value < spec.low:
        return True
    if spec.abs_high is not None and abs(value) > spec.abs_high:
        return True
    return False


def _cleared(value, spec: AlarmSpec) -> bool:
    # "cleared" means back inside every deadband whose limit type is actually
    # configured; a limit type that isn't configured never blocks clearing.
    ok = True
    if spec.high is not None:
        clear_high = spec.clear_high if spec.clear_high is not None else spec.high
        ok = ok and value <= clear_high
    if spec.low is not None:
        clear_low = spec.clear_low if spec.clear_low is not None else spec.low
        ok = ok and value >= clear_low
    if spec.abs_high is not None:
        clear_abs_high = spec.clear_abs_high if spec.clear_abs_high is not None else spec.abs_high
        ok = ok and abs(value) <= clear_abs_high
    return ok


def _worse(value, peak, spec: AlarmSpec) -> bool:
    if peak is None:
        return True
    if spec.high is not None:
        return value > peak
    if spec.abs_high is not None:
        return abs(value) > abs(peak)
    if spec.low is not None:
        return value < peak
    return False


def _limit_message(channel_id, spec: AlarmSpec, value) -> str:
    if spec.high is not None and value > spec.high:
        return f"{channel_id}: {value} > {spec.high}"
    if spec.abs_high is not None and abs(value) > spec.abs_high:
        return f"{channel_id}: |{value}| > {spec.abs_high}"
    if spec.low is not None and value < spec.low:
        return f"{channel_id}: {value} < {spec.low}"
    return f"{channel_id}: {value} out of range"


class AlarmEvaluator:
    '''Owns per-channel runtime state across scans. Call evaluate_channel() once per
    channel per scan tick -- even with an empty samples list -- so staleness keeps
    being checked for channels with no new data.'''

    def __init__(self):
        self._states: dict[str, ChannelAlarmState] = {}

    def state_for(self, channel_id: str) -> ChannelAlarmState:
        return self._states.setdefault(channel_id, ChannelAlarmState(channel_id=channel_id))

    def evaluate_channel(self, channel_id, spec: AlarmSpec | None, samples, now: float, log_interval_s: float):
        '''samples: [(timestamp, raw_value), ...] newly arrived since the previous
        scan, in chronological order -- every sample is folded through in order so an
        excursion between ticks is never missed, not just the newest one. Always uses
        the raw channel value; offset correction is a display-only concern of the
        plot layer and must never reach here.'''
        state = self.state_for(channel_id)
        transitions = []

        for timestamp, value in samples:
            state.last_timestamp = timestamp
            state.last_value = value
            transitions += self._evaluate_sample(state, spec, value, timestamp)

        transitions += self._evaluate_staleness(state, spec, now, log_interval_s)
        return transitions

    def acknowledge(self, channel_id, now: float):
        state = self._states.get(channel_id)
        if state is None or not state.latched:
            return []

        if state.state in (AlarmState.ALARM, AlarmState.STALE):
            # Still actively breaching: dismiss the banner only. The tile stays
            # colored and the condition keeps evaluating live.
            state.acknowledged = True
            return [AlarmTransition(channel_id=channel_id, from_state=state.state, to_state=state.state,
                                     timestamp=now, value=state.last_value,
                                     message=f"{channel_id}: acknowledged (still active)")]

        # Already back to OK/NO_DATA and latched -- this is the "cleared,
        # unacknowledged" case; acknowledging it fully removes the episode.
        peak, trip = state.peak_value, state.trip_timestamp
        state.latched = False
        state.acknowledged = True
        state.peak_value = None
        state.peak_timestamp = None
        state.trip_timestamp = None
        return [AlarmTransition(channel_id=channel_id, from_state=state.state, to_state=state.state,
                                 timestamp=now, value=state.last_value,
                                 message=f"{channel_id}: acknowledged (was cleared, peak {peak} at {trip})")]

    def acknowledge_all(self, now: float):
        transitions = []
        for channel_id in list(self._states.keys()):
            transitions += self.acknowledge(channel_id, now)
        return transitions

    def _change_state(self, state: ChannelAlarmState, new_state: AlarmState, timestamp, value, message) -> AlarmTransition:
        old_state = state.state
        state.state = new_state
        return AlarmTransition(channel_id=state.channel_id, from_state=old_state, to_state=new_state,
                                timestamp=timestamp, value=value, message=message)

    def _evaluate_sample(self, state: ChannelAlarmState, spec: AlarmSpec | None, value, timestamp):
        transitions = []

        # NaN must never satisfy an OK/ALARM comparison -- check it before any
        # threshold math runs. An intentionally-off gauge is normal: NO_DATA never
        # latches, and entering it drops any earlier unacknowledged episode.
        if isinstance(value, float) and math.isnan(value):
            if state.state != AlarmState.NO_DATA:
                state.latched = False
                state.acknowledged = True
                state.peak_value = None
                state.peak_timestamp = None
                state.trip_timestamp = None
                transitions.append(self._change_state(state, AlarmState.NO_DATA, timestamp, value,
                                                        f"{state.channel_id}: no data"))
            return transitions

        if state.state == AlarmState.NO_DATA:
            state.consecutive_breach = 0
            transitions.append(self._change_state(state, AlarmState.OK, timestamp, value,
                                                    f"{state.channel_id}: data resumed"))
        elif state.state == AlarmState.STALE:
            # A fresh sample resolves staleness immediately (no debounce needed for
            # that). This keeps the episode latched (STALE already raised the
            # banner) but starts a fresh "cleared, pending ack" sub-episode so the
            # operator still sees that the feed came back, even if the still-stale
            # condition had already been acknowledged.
            state.consecutive_breach = 0
            state.acknowledged = False
            transitions.append(self._change_state(state, AlarmState.OK, timestamp, value,
                                                    f"{state.channel_id}: data resumed after stale"))

        if spec is None:
            return transitions  # no thresholds configured for this channel

        breached = _breach(value, spec)

        if state.state == AlarmState.OK:
            if breached:
                state.consecutive_breach += 1
                if state.consecutive_breach >= spec.consecutive_samples:
                    state.peak_value = value
                    state.peak_timestamp = timestamp
                    state.trip_timestamp = timestamp
                    state.latched = True
                    state.acknowledged = False
                    transitions.append(self._change_state(state, AlarmState.ALARM, timestamp, value,
                                                            _limit_message(state.channel_id, spec, value)))
            else:
                state.consecutive_breach = 0

        elif state.state == AlarmState.ALARM:
            if breached:
                if _worse(value, state.peak_value, spec):
                    state.peak_value = value
                    state.peak_timestamp = timestamp
            elif _cleared(value, spec):
                peak, trip = state.peak_value, state.trip_timestamp
                state.consecutive_breach = 0
                # Fresh "cleared" sub-episode: even if the active alarm had already
                # been acknowledged, clearing re-raises the banner (this is what
                # makes a re-trigger after clearing show up again).
                state.acknowledged = False
                transitions.append(self._change_state(state, AlarmState.OK, timestamp, value,
                                                        f"{state.channel_id}: cleared, peak {peak} at {trip}"))
            # else: inside the deadband strip between the clear and trip thresholds
            # -- stays ALARM, no transition.

        return transitions

    def _evaluate_staleness(self, state: ChannelAlarmState, spec: AlarmSpec | None, now: float, log_interval_s: float):
        if state.last_timestamp is None:
            return []  # never seen any data at all yet -- nothing to call stale

        multiplier = spec.stale_multiplier if spec is not None else AlarmSpec().stale_multiplier
        threshold = multiplier * log_interval_s

        if state.state != AlarmState.STALE and (now - state.last_timestamp) > threshold:
            state.latched = True
            state.acknowledged = False
            age = now - state.last_timestamp
            return [self._change_state(state, AlarmState.STALE, now, state.last_value,
                                        f"{state.channel_id}: stale (no data for {age:.0f}s)")]
        return []
