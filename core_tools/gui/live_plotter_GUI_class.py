from pyqtgraph.Qt import QtWidgets, QtCore
import pyqtgraph as pg
import sys
import os
import time
import math
import datetime
import pandas as pd
import numpy as np
import subprocess
import threading
import platform
import serial.tools.list_ports
import json

from .get_data_for_GUI import get_n_XY_datapoints
from .models import (Channel, Plot, LoggerControl, SetpointControl, SerialBus,
                     Interlock, FillControl, AggregateTile)
from .decimate import decimate_min_max
from .data_cache import ScanRunnable
from .palette import channel_palette
from core_tools.notes import NoteStore, visible_notes
from core_tools.alarms import AlarmEvaluator, AlarmSpec, AlarmState, DisplayStatus, display_status

'''Class to handle live plotting and add various controls/buttons in a Qt GUI application.'''

# Curve colors for LiveTab.add_plot()'s multi-channel plots only -- these overlay two
# or three channels at most, where a short hand-picked cycle is clearer than a
# generated one. The VMM overlay needs 32 distinct colors and generates its own
# palette instead (core_tools/gui/palette.py); do not merge the two.
COLOR_CYCLE = ['y', 'c', 'm', 'r', 'g', 'b', 'w']

# 'running'/'stopped'/'crashed' are a logger's subprocess lifecycle; a setpoint
# control has no lifecycle to show (its subprocess is one-shot), so it reuses these
# for the outcome of the last command -- green for acknowledged, red for rejected,
# grey for "nothing sent yet" -- plus 'sending' while one is in flight.
LED_COLORS = {'running': '#2ecc71', 'stopped': '#95a5a6', 'crashed': '#e74c3c', 'sending': '#f39c12'}
ALARM_COLOR = '#e74c3c'

# A setpoint command opens the serial port, sleeps 1s for the device to initialize,
# writes, and reads with a 1s timeout, so it normally returns in ~2s. This bound is
# what keeps a controller that never answers from leaving the Set button disabled
# forever; it must stay comfortably above that normal round trip.
SETPOINT_TIMEOUT_S = 15

# A tripped interlock's safe-value command is retried until the controller
# acknowledges it, because a safety action that quietly failed is worse than none at
# all. Bounded, though: against a dead port every attempt fails instantly, and an
# unbounded retry would bury the event log in the one situation where the operator
# most needs to read it. After the cap the interlock stops commanding and says, in
# red and in the log, that the gas has to be shut manually.
INTERLOCK_RETRY_DELAY_S = 2.0
INTERLOCK_MAX_ATTEMPTS = 5

# How many consecutive rejected frames a bus unit may have before its control goes
# red. Frame errors (a mis-addressed reply, a short read) are usually transient and
# the next poll clears them; a port failure is not debounced at all, because a port
# that will not open is broken right now.
BUS_POLL_ERRORS_BEFORE_FAULT = 3

# How often the alarm scanner reads every registered channel, and the FLOOR on how
# many trailing rows each read fetches -- a floor for alarm evaluation's benefit
# only, never a count of points to draw (see rows_to_fetch). Deliberately
# independent of any plot's own timer (a paused plot must not pause its channels'
# alarm evaluation) and, for now, independent of the per-plot update() reads too --
# both re-read from disk until the shared data cache lands, which is a known,
# temporary duplication of file reads.
ALARM_SCAN_INTERVAL_MS = 1000
ALARM_LOOKBACK_ROWS = 50

# Global time-window selector (§7) -- replaces the old per-tab "# data points shown"
# dropdowns. n = ceil(window_s / channel.log_interval_s) rows are fetched per
# channel and decimated down to DECIMATION_CAP points if that exceeds it.
WINDOW_OPTIONS = [('1m', 60), ('5m', 300), ('15m', 900), ('1h', 3600), ('6h', 21600), ('24h', 86400)]
DEFAULT_WINDOW_S = 300
DECIMATION_CAP = 20000

# Qt's default splitter handle is ~4px, which is hard to grab precisely; widen every
# draggable splitter in the app (control dock, event log, VMM tab's tile/plot divider).
SPLITTER_HANDLE_WIDTH = 8

# Width of a status-strip tile's value readout. The strip sits directly in
# main_layout, sharing a fixed total height with main_splitter, so anything that
# lets a tile's size follow its text feeds every scan tick's text change straight
# into the window's layout: the strip's height shifts and the tabs/control dock
# below it lose that space. Pinning the width (and eliding instead of wrapping)
# keeps the strip's geometry constant whatever the text does, and still avoids the
# window-width floor an unwrapped, unconstrained label would impose -- see AlarmBanner.
STATUS_TILE_VALUE_WIDTH = 150

# Side of the color swatch on each VMM tile. The tile grid is the overlay plot's
# legend (see VMMTab), so this needs to be big enough to read a hue off at a glance
# without widening a 4-column grid of 32 tiles.
SWATCH_SIZE = 12

# Width of a plot header's follow/frozen indicator. Fixed so that flipping between
# "FOLLOWING" and "FROZEN" can't resize the header row (and through it the plot's
# grid cell) -- same reasoning as STATUS_TILE_VALUE_WIDTH above.
FOLLOW_INDICATOR_WIDTH = 74


# Two different row counts, and conflating them is what made a "1m" window show ~100s
# of data on every 2s channel:
#
#   rows_for_window() is how many rows the WINDOW covers -- what a plot should draw.
#   rows_to_fetch()   is how many rows the scan READS, which also has to satisfy
#                     alarm evaluation's ALARM_LOOKBACK_ROWS floor.
#
# The floor is larger than the window whenever window_s / log_interval_s < 50 (a 1m
# window on a 2s channel wants 30 rows), so the two must not be the same number: the
# scan reads the larger one and _on_scan_finished() trims to the smaller one before
# drawing. Keep it that way -- reading only rows_for_window() rows would silently
# shorten alarm evaluation's history at short windows, which is the failure the floor
# exists to prevent.
# A dot per sample, so the actual sample positions are visible and not just the trend
# line joining them. Shown only while they are far enough apart to read: a plot is a
# few hundred pixels wide, so beyond a few hundred points the dots stop being separate
# marks and become a solid smear that hides the very trace it is drawn on -- and past
# DECIMATION_CAP they would not even be real samples, since decimation replaces them
# with min/max per bucket. Above the cap the line alone carries the trend.
#
# Cost measured at ~0.4ms per curve for a line vs ~10.5ms with 20000 dots; with every
# plot redrawing each scan tick, always-on dots would spend a large slice of every
# second on marks nobody can resolve.
POINT_MARKER_SIZE = 5
# Minimum horizontal pixels per dot for the dots to read as separate marks. Judged
# against the plot's actual width rather than a fixed point count, because the same
# 450 points are legible on a maximised plot and a solid smear on a narrow one -- and
# the operator resizes the dock, the window and the tab splitters constantly. At 3px
# the default 5m window (150 points on a ~580px plot) keeps its dots and 15m does not,
# which is about where they stop being separable anyway.
POINT_MARKER_MIN_SPACING_PX = 3
# Fallback only, for the first redraw before the plot has been laid out and has a
# width to measure. Also an upper bound: past this, dots are a repaint cost with
# nothing to show for it whatever the window size.
SYMBOL_MAX_POINTS = 500


def style_point_markers(curve, color):
    """Configure a curve's dots once, at creation. Size/brush/pen persist across
    setSymbol(None), so showing and hiding later is a single call."""
    curve.setSymbolSize(POINT_MARKER_SIZE)
    curve.setSymbolBrush(color)   # filled, so a dot reads as a dot at 5px
    curve.setSymbolPen(None)      # no outline -- an outline at this size just muddies it
    curve.setSymbol(None)         # off until the first update decides


def point_marker_budget(curve):
    """How many points this curve can show dots for, from its width on screen."""
    view = curve.getViewBox()
    width = view.width() if view is not None else 0
    if not width:
        return SYMBOL_MAX_POINTS  # not laid out yet; the next redraw measures properly
    return min(int(width / POINT_MARKER_MIN_SPACING_PX), SYMBOL_MAX_POINTS)


def set_point_markers(curve, y_values):
    """Show or hide a curve's dots for the data about to be drawn.

    Counts the points that will actually be MARKED -- the finite ones -- not the length
    of the array. A window where most samples are NaN draws far fewer dots than it has
    rows, and a channel that is all NaN draws none at all: an intentionally-off gauge
    (the low-range OV gauge above ~1 Torr) reads as NaN for every row in the window.

    Zero finite points must turn the dots OFF rather than leave an empty scatter item
    behind. pyqtgraph computes a scatter's bounds with np.nanmin/np.nanmax, which on an
    all-NaN array warns "All-NaN slice encountered" on every redraw -- once per curve
    per scan tick, which floods the console the logger prints share.

    Only touches the curve when the answer changes: setSymbol triggers a repaint, and
    this runs for every curve on every scan tick.
    """
    n_points = int(np.isfinite(np.asarray(y_values, dtype=float)).sum()) if len(y_values) else 0
    wanted = 0 < n_points <= point_marker_budget(curve)
    if getattr(curve, '_markers_on', None) is wanted:
        return
    curve._markers_on = wanted
    curve.setSymbol('o' if wanted else None)


def rows_for_window(window_s, log_interval_s):
    return max(2, math.ceil(window_s / log_interval_s))


def rows_to_fetch(window_s, log_interval_s):
    return max(ALARM_LOOKBACK_ROWS, rows_for_window(window_s, log_interval_s))


def tail(values, count):
    '''The last `count` items of a pandas Series or a plain array. Series.iloc is
    positional; bare [] slicing on a Series whose index is integer-typed is ambiguous
    between positional and label-based.'''
    return values.iloc[-count:] if hasattr(values, 'iloc') else values[-count:]


def apply_style(widget, style):
    '''setStyleSheet() re-polishes the widget -- re-deriving its frame width, contents
    margins and font metrics -- even when the sheet is byte-identical to the current
    one. Every refresh path here runs once per scan tick over every tile in the app,
    so re-applying unconditionally means re-computing all of their geometry every
    second. Only touch the sheet when it actually changed.'''
    if widget.styleSheet() != style:
        widget.setStyleSheet(style)


def format_age(age):
    '''A staleness duration as a short, bounded string. Stepping the unit keeps the
    digit count small: an age rendered as raw seconds grows a character every power
    of ten, and every consumer of this string sits in a label whose size follows its
    text, so an unbounded string there quietly resizes the window's layout.'''
    if age < 600:
        return f"{age:.0f}s"
    if age < 180 * 60:
        return f"{age / 60:.0f}m"
    if age < 72 * 3600:
        return f"{age / 3600:.0f}h"
    return f"{age / 86400:.0f}d"


def format_channel_value(channel, state, status, offset, now):
    '''(text, stylesheet) for a channel's current value -- shared by per-plot value
    readouts, status-strip tiles, and (later) the overview tab, so all three render
    a channel's state identically.'''
    value = state.last_value
    if value is None or value != value:  # NaN-safe for any numeric type
        return "—", "color: grey;"

    displayed = value + offset
    text = f"{displayed:.3g} {channel.units}"
    if status == DisplayStatus.ALARM:
        return text, f"color: {ALARM_COLOR}; font-weight: bold;"
    if status == DisplayStatus.STALE:
        age = (now - state.last_timestamp) if state.last_timestamp else 0
        return f"{text} (stale {format_age(age)})", "color: grey;"
    if status == DisplayStatus.CLEARED:
        return text, "color: #b8860b;"
    return text, ""


# Follow/frozen indicator (§1). A plot's ViewBox turns autorange off the moment the
# user zooms or pans it, after which new data keeps arriving but the visible range
# stops tracking it -- an operator can zoom in, walk away, and come back to a plot
# that looks live while showing a frozen window into the past. FOLLOWING is
# deliberately quiet (this is the normal state, it shouldn't compete for attention);
# FROZEN is amber and obvious. Amber, not red: red is the alarm color everywhere else
# in this GUI and a frozen plot is not an alarm condition.
FROZEN_COLOR = '#f39c12'
FOLLOW_STYLE = 'color: grey; border: none; background: transparent; font-size: 10px;'
FROZEN_STYLE = (f'color: {FROZEN_COLOR}; border: 1px solid {FROZEN_COLOR}; '
                'background: transparent; font-weight: bold; font-size: 10px;')


def make_follow_indicator(on_click):
    '''The clickable FOLLOWING/FROZEN indicator. A button rather than a label so it's
    obviously clickable and keyboard-reachable; styled flat so FOLLOWING doesn't read
    as a second action button sitting next to pause.'''
    button = QtWidgets.QPushButton()
    button.setCursor(QtCore.Qt.PointingHandCursor)
    button.clicked.connect(lambda _: on_click())
    set_follow_indicator(button, following=True, paused=False)
    return button


def set_follow_indicator(button, following, paused):
    '''Text/style for one indicator.

    The indicator answers one question -- "am I looking at live data?" -- so pausing
    reads as FROZEN too. Pausing stops new data reaching the curve at all, which
    leaves exactly the hazard this indicator exists for: a plot that looks live while
    showing a fixed window into the past. An amber badge that stayed quiet through a
    pause would be answering a narrower question than the one an operator is asking.

    Pause and freeze stay tellable apart: the pause toggle beside this still shows
    ⏸/▶, and the tooltip here names the actual reason (or both reasons).

    The text is fixed-length either way ("FOLLOWING" vs "FROZEN" both render inside
    the same fixed width) so flipping it can never resize the header row it sits in --
    same constraint as the status strip's tiles.'''
    live = following and not paused
    text = 'FOLLOWING' if live else 'FROZEN'
    if button.text() != text:
        button.setText(text)

    if live:
        tooltip = 'Following live data. Zoom or pan to inspect history (which freezes it).'
    elif paused and not following:
        tooltip = ('FROZEN: paused, and zoomed or panned away from the live range. '
                   'Click to resume live view.')
    elif paused:
        tooltip = 'FROZEN: paused, so no new data is being drawn. Click to resume live view.'
    else:
        tooltip = ('FROZEN: still receiving data, but not scrolling to show it. '
                   'Click to resume live view.')
    button.setToolTip(tooltip)
    apply_style(button, FOLLOW_STYLE if live else FROZEN_STYLE)


def is_following(plot_widget):
    '''Whether a plot's view is still tracking new data, read from the ViewBox itself
    rather than remembered from one signal.

    Autorange is turned off by a zoom or pan, and back ON by two things besides our
    own indicator: pyqtgraph's auto-scale button (the small "A" that appears at the
    bottom left once a plot is zoomed) and the right-click menu's X/Y "Auto"
    checkboxes. Tracking only sigRangeChangedManually caught the freeze but neither
    way out of it, so a plot that had resumed tracking kept a stale FROZEN badge.
    Deriving the answer from the ViewBox can't drift out of sync like that.

    Both axes have to be tracking: X is what makes a plot follow time, but a Y range
    left pinned to an old zoom hides live data just as effectively.'''
    return all(plot_widget.getViewBox().autoRangeEnabled())


def resume_following_on(plot_widget):
    '''Snap a plot back to live. enableAutoRange() re-derives the visible range from
    the data's own extent, which *is* the current time window (every channel is
    fetched for exactly that window), so this needs no explicit setXRange -- and
    couldn't use one anyway: setXRange() turns X autorange back off, which is the very
    state being escaped here. Y autorange is restored too, since a manual zoom
    disabled both and leaving Y pinned to an old range still hides live data.'''
    plot_widget.getViewBox().enableAutoRange()


# Operator note markers (§2). Deliberately unlike the alarm threshold lines, which
# are dashed red: a note is not a limit and must never be mistaken for one. Vertical
# dotted blue, and slightly wider than a hairline so there's something to hover.
NOTE_MARKER_COLOR = '#5dade2'


def sync_note_markers(plot_widget, pool, notes):
    '''Point one plot's marker pool at `notes` ([(x, text), ...] from
    core_tools.notes.visible_notes).

    The pool is reused, never rebuilt: with a dozen plots refreshing once a second,
    creating and destroying line items every tick is churn you can feel. It only ever
    grows -- to the high-water mark of simultaneously-visible notes -- and surplus
    lines are hidden rather than removed.

    ignoreBounds=True keeps a marker out of the ViewBox's autorange calculation. A
    marker at the edge of the window must not stretch the X range, or the act of
    adding a note would move every following plot's view.'''
    while len(pool) < len(notes):
        line = pg.InfiniteLine(angle=90, movable=False,
                               pen=pg.mkPen(NOTE_MARKER_COLOR, width=2, style=QtCore.Qt.DotLine))
        plot_widget.addItem(line, ignoreBounds=True)
        pool.append(line)

    for line, (x, text) in zip(pool, notes):
        line.setPos(x)
        line.setToolTip(text)  # hovering a marker shows the note
        line.setVisible(True)
    for line in pool[len(notes):]:
        line.setVisible(False)


def last_line(text):
    """Last non-blank line of a subprocess's captured output, or '' if there is none."""
    lines = [line.strip() for line in (text or '').splitlines() if line.strip()]
    return lines[-1] if lines else ''


def setpoint_acknowledged(exit_code, reply):
    """Did the MFC actually take the setpoint?

    The exit code alone cannot answer this: the Alicat control script prints
    "ERROR during set setpoint, output: ..." and still exits 0 when the controller's
    reply isn't a valid data frame -- wrong unit id, setpoint source configured for
    analog rather than Serial/Front Panel, or nothing on the other end of the line.
    Treating exit 0 as success would report a command that never landed as applied,
    with the controller still at its old flow. So both have to hold: the process
    ran cleanly AND the script says the controller acknowledged.

    Matched on the leading word alone, deliberately. The rest of that sentence is a
    human-readable message that has already been reworded once ('Successfully set
    setpoint' -> 'Successfully set Alicat MFC setpoint'), and a longer prefix silently
    turned every successful command into a reported failure -- which for the interlock
    means crying wolf on a safe-value command that actually landed.
    """
    return exit_code == 0 and reply.lower().startswith('successfully')


def interlock_should_trip(transition, trigger_channel_ids, trip_on_stale):
    """Does this alarm transition fire an interlock watching these channels?

    Entering ALARM always trips. Entering STALE trips only if the interlock was
    declared with trip_on_stale: a channel that stopped reporting isn't reading high,
    but it isn't reading safe either, and an interlock that guards a vessel can't
    treat "I don't know" as "it's fine".

    NO_DATA deliberately does NOT trip. The evaluator reaches it on NaN readings,
    which core_tools/alarms.py treats as a normal, intentionally-off gauge -- tripping
    on it would shut the gas every time a gauge is switched off on purpose.

    Only transitions *into* a state count. The evaluator emits a transition per state
    change, so a channel sitting in ALARM produces nothing further and an interlock
    can't be re-fired by an alarm it already acted on.
    """
    if transition.channel_id not in trigger_channel_ids:
        return False
    if transition.to_state == AlarmState.ALARM:
        return True
    return trip_on_stale and transition.to_state == AlarmState.STALE


def interlock_reset_blockers(alarm_states):
    """Which trigger channels are not confirmed good, as [(channel_id, reason)].

    Used for two things that turn out to be the same question: whether an interlock
    may arm, and whether a tripped one may be reset. Both need the vessel's pressure
    to be currently *knowable* -- ALARM and STALE block, because letting gas into a
    vessel reading high, or one whose readings have stopped arriving, is exactly what
    the interlock exists to prevent.

    A channel in NO_DATA does NOT block, as long as some other trigger channel is
    reading. NO_DATA is how core_tools/alarms.py represents a deliberately-off gauge
    ('Off' in the log -> NaN), and the 40L's low-range OV gauge is switched off above
    ~1 Torr -- so requiring every channel to read OK meant the interlock could never
    arm during normal operation, and would not have tripped at 765 Torr. That also
    contradicted the trip rule, which already ignores NO_DATA for the same reason: an
    intentionally-off gauge is not a fault, it is just not the gauge in use right now.

    What must never happen is arming with no usable gauge at all, so when NOTHING is
    reading OK every non-OK channel blocks, NO_DATA included.

    A channel that has produced no data at all always blocks. ChannelAlarmState starts
    at OK before anything has been read, so reading that default as a good sample would
    arm the interlock on a channel nobody has heard from -- and it would then trip the
    moment the scan noticed the log file was stale, which is every launch made before
    the logger is started.

    An empty list means armed / resettable.
    """
    any_reading = any(state.state == AlarmState.OK and state.last_timestamp is not None
                      for state in alarm_states.values())
    blockers = []
    for channel_id, state in alarm_states.items():
        if state.last_timestamp is None:
            blockers.append((channel_id, 'no data yet'))
        elif state.state in (AlarmState.ALARM, AlarmState.STALE):
            blockers.append((channel_id, state.state.value))
        elif state.state == AlarmState.NO_DATA and not any_reading:
            blockers.append((channel_id, state.state.value))
    return blockers


def fill_stage_for(pressure, slow_at, target):
    """Which stage a fill should be in at this pressure: 'fast', 'slow' or 'done'.

    Target is checked before the handover, so a reading that jumps straight past both
    (a coarse logging interval, a pressure spike) finishes the fill rather than
    dropping to the slow rate and carrying on filling past the target.

    Boundaries are inclusive: at exactly the target the fill is done. Stopping a
    fraction early is harmless; the vessel coasts up a little after the valve shuts
    anyway. Continuing at exactly the target is not.
    """
    if pressure >= target:
        return 'done'
    if pressure >= slow_at:
        return 'slow'
    return 'fast'


def fill_flow_for(stage, fast_flow, slow_flow, previous_flow=None):
    """The flow a stage calls for, never higher than what the fill has already settled to.

    The ratchet is deliberate. Pressure dithering across the handover threshold would
    otherwise swing the MFC between the two rates, and a fill whose pressure is FALLING
    means something is wrong -- a leak, or someone pumping -- which is not a reason to
    open the valve wider. Once this fill has eased off, it only ever eases off further.
    """
    wanted = {'fast': fast_flow, 'slow': slow_flow, 'done': 0.0, 'aborted': 0.0}[stage]
    if previous_flow is None:
        return wanted
    return min(wanted, previous_flow)


def fill_settings_error(fast_flow, slow_flow, slow_at, target, pressure, alarm_limit):
    """Why this fill must not start, or None if it may. Checked at the moment Engage is
    pressed, against the values in the boxes and the pressure right now."""
    if pressure is None:
        return 'no usable pressure reading'
    if slow_at >= target:
        return (f'the slow-down pressure ({slow_at:g}) must be below the target '
                f'({target:g}), or the slow stage never runs')
    if slow_flow > fast_flow:
        return f'the slow rate ({slow_flow:g}) must not exceed the fast rate ({fast_flow:g})'
    if slow_flow <= 0:
        return 'the slow rate must be above zero, or the fill can never reach its target'
    if pressure >= target:
        return f'already at or above the target ({pressure:g} >= {target:g})'
    # An alarm limit on the pressure being filled is a hard ceiling: filling to or past
    # it would trip the over-pressure interlock mid-fill, shut the gas and latch the
    # setpoint control. Better to refuse now than to discover it at the top of a fill.
    if alarm_limit is not None and target >= alarm_limit:
        return (f'the target ({target:g}) is at or above the over-pressure alarm '
                f'({alarm_limit:g}); the fill would trip the interlock')
    return None


def channels_fed_by(channels, log_filepath):
    """Ids of the channels a logger writing `log_filepath` feeds.

    A logger control and a channel are declared independently in launch_GUI.py and
    never name each other -- the file they share is the only link between them. It is
    what lets a logger's actual rate reach the channels that depend on it, instead of
    each side believing whatever it was declared with.
    """
    return [channel_id for channel_id, channel in channels.items()
            if channel.filepath == log_filepath]


def _check_transport(kind, control_id, bus_id, buses, script, port):
    """A control talks over a shared bus or over its own subprocess -- never both, and
    never neither.

    Declaring a bus AND a script/port used to be accepted and the script/port silently
    ignored, which left arguments sitting in launch_GUI.py that read as load-bearing
    and were not: someone would later 'fix' a bug by editing a script nothing runs.
    Raising here keeps a declaration from describing something the control doesn't do.
    """
    if bus_id is not None:
        if bus_id not in buses:
            raise ValueError(f"{kind} {control_id!r}: no serial bus {bus_id!r} "
                             f"(declare it before the controls that share it; known: {sorted(buses)})")
        extra = [name for name, value in (('script', script), ('port', port)) if value is not None]
        if extra:
            raise ValueError(f"{kind} {control_id!r}: {' and '.join(extra)} cannot be combined with "
                             f"bus={bus_id!r} -- the bus owns the port and does the talking, "
                             f"so these would be ignored")
        return
    missing = [name for name, value in (('script', script), ('port', port)) if value is None]
    if missing:
        raise ValueError(f"{kind} {control_id!r}: needs {' and '.join(missing)} "
                         f"(or bus=... to attach it to a shared serial bus)")


def limit_description(alarm):
    if alarm is None:
        return ""
    if alarm.high is not None:
        return f"limit {alarm.high}"
    if alarm.abs_high is not None:
        return f"limit ±{alarm.abs_high}"
    if alarm.low is not None:
        return f"limit {alarm.low}"
    return ""


class LivePlotter:
    def __init__(self, win_title):
        # Create the main Qt application
        self.app = QtWidgets.QApplication(sys.argv)

        # Main window setup
        self.main_window = QtWidgets.QMainWindow()
        self.main_widget = QtWidgets.QWidget()
        self.main_layout = QtWidgets.QVBoxLayout()
        self.main_widget.setLayout(self.main_layout)
        self.main_window.setCentralWidget(self.main_widget)
        self.main_window.setWindowTitle(win_title)

        self.tab_objects = {}  # tab_name -> LiveTab object
        self.channels = {}     # channel_id -> Channel, registered once, shared across all tabs
        self.channel_plots = {}  # channel_id -> [(LiveTab, plot_id), ...], for alarm-driven visuals
        self.overview_tab = None  # set by build_overview_tab(), if the caller wants one

        self.settings = QtCore.QSettings('40L-TPC', 'RunControlGUI')
        self.event_log = EventLog()

        # Operator notes (§2). Held as absolute timestamps; each plot's marker X is
        # recomputed from them every scan tick, which is what makes markers drift
        # left along with the data -- see _refresh_note_markers().
        self.note_store = NoteStore()
        self.notes = self.note_store.load_recent(now=time.time())
        if self.notes:
            self.log(f"Restored {len(self.notes)} operator note(s) from the last 24h", level='INFO')
        self.window_seconds = DEFAULT_WINDOW_S  # overwritten below once ControlDock restores any saved selection

        # Alarm banner (hidden when nothing is active) and the status strip are
        # always visible above the tabs, regardless of which tab is selected.
        self.alarm_banner = AlarmBanner(self)
        self.status_strip = StatusStrip(self)

        # Tabs (left) and the persistent control dock (right) sit side by side and
        # also stay visible regardless of which tab is selected.
        self.tabs = QtWidgets.QTabWidget()
        self.control_dock = ControlDock(self)

        self.main_splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self.main_splitter.setHandleWidth(SPLITTER_HANDLE_WIDTH)
        self.main_splitter.addWidget(self.tabs)
        self.main_splitter.addWidget(self.control_dock)
        self.main_splitter.setStretchFactor(0, 4)
        self.main_splitter.setStretchFactor(1, 1)

        # The control dock is the one collapsible region left (drag the splitter
        # handle to 0 to collapse); the event log used to sit in a second collapsible
        # pane below this but now lives in its own "Event Terminal" tab instead (added
        # in run(), so it lands after every tab launch_GUI.py creates).
        self.main_layout.addWidget(self.alarm_banner)
        self.main_layout.addWidget(self.status_strip)
        self.main_layout.addWidget(self.main_splitter)

        self._restore_layout()

        # One scan timer drives everything: alarm evaluation (independent of any
        # plot's own pause state, see core_tools/alarms.py and §4.4) AND every plot's
        # curve redraw. The actual file reads happen on a QThreadPool worker thread
        # (core_tools/gui/data_cache.py) so the GUI thread never blocks on disk; a
        # reentrancy flag drops a tick rather than queueing if the previous scan's
        # background job hasn't finished yet.
        self.alarm_evaluator = AlarmEvaluator()
        self._alarm_last_ts = {}  # channel_id -> newest absolute timestamp already evaluated
        self._scan_in_flight = False
        # The in-flight ScanRunnable, held here for the duration of the scan. This is
        # NOT an unused attribute -- do not delete it. QThreadPool takes C++-side
        # ownership of the runnable (autoDelete defaults to True), but
        # runnable.signals is a parentless QObject attribute of a Python object that
        # nothing else references once _start_scan() returns, so Python is free to
        # collect it while the worker thread is still running -- and the eventual
        # .emit() on that thread then raises "wrapped C/C++ object of type
        # ScanWorkerSignals has been deleted". Only one scan is ever in flight
        # (_scan_in_flight guarantees it), so one attribute is enough.
        self._active_scan_runnable = None
        self.scan_timer = QtCore.QTimer()
        self.scan_timer.timeout.connect(self._start_scan)
        self.scan_timer.start(ALARM_SCAN_INTERVAL_MS)

        # Calls the cleanup function when the application is about to quit so that all running subprocesses are terminated
        self.app.aboutToQuit.connect(self.cleanup)

    def log(self, message, level='INFO'):
        self.event_log.add_line(level, message)

    # One operator note: an event-log line at NOTE level (so it shows up in the Event
    # Terminal and in that day's on-disk log alongside everything else), a row in
    # operator_notes.csv, and a marker on every plot.
    def add_operator_note(self, note):
        note = note.strip()
        if not note:
            return None
        now = time.time()

        # Event log first: it's the record that can't fail here, so a note is never
        # lost just because the CSV couldn't be written.
        self.log(note, level='NOTE')
        try:
            self.note_store.append(note, timestamp=now)
        except OSError as e:
            self.log(f"could not append to {self.note_store.filepath}: {e}", level='ERROR')

        self.notes.append((now, note))
        self._refresh_note_markers(now)  # don't make the operator wait a tick to see it
        return now

    # Reposition every plot's note markers for the current `now`. Notes are stored
    # absolute but plots use a "seconds since present" X axis, so each marker sits at
    # note_time - now and has to be recomputed on every tick -- drawing at a fixed X
    # would look right for exactly one tick and be silently wrong from then on.
    # Notes are append-only at runtime, so with none recorded there is nothing drawn
    # and nothing to hide.
    def _refresh_note_markers(self, now):
        if not self.notes:
            return
        markers = visible_notes(self.notes, now, self.window_seconds)
        for tab in self.tab_objects.values():
            if isinstance(tab, LiveTab):
                for plot in tab.plots.values():
                    sync_note_markers(plot.plot_widget, plot.note_lines, markers)
            elif isinstance(tab, VMMTab):
                sync_note_markers(tab.overlay_widget, tab.note_lines, markers)

    def _restore_layout(self):
        main_sizes = self.settings.value('main_splitter_sizes')
        if main_sizes:
            self.main_splitter.setSizes([int(s) for s in main_sizes])

    def _save_layout(self):
        self.settings.setValue('main_splitter_sizes', self.main_splitter.sizes())

    # Register a data source once so it can be referenced by id from any tab's plots,
    # read for the status strip, or evaluated for alarms -- with or without a plot.
    def add_channel(self, id, label, long_label, filepath, datatype, units, log_interval_s, alarm=None, vmm_num=None, overview_group=None):
        channel = Channel(
            id=id, label=label, long_label=long_label, filepath=filepath, datatype=datatype,
            units=units, log_interval_s=log_interval_s, alarm=alarm, vmm_num=vmm_num, overview_group=overview_group,
        )
        self.channels[id] = channel
        return channel

    # Kick off one background read of every registered channel (one disk read per
    # distinct file -- see data_cache.py), regardless of which plots are paused or
    # which tab is selected. Dropped rather than queued if the previous scan's
    # background job is still running.
    def _start_scan(self):
        if self._scan_in_flight:
            return
        self._scan_in_flight = True

        requests = []
        for channel_id, channel in self.channels.items():
            n = rows_to_fetch(self.window_seconds, channel.log_interval_s)
            requests.append((channel_id, channel.filepath, n, channel.datatype, channel.vmm_num))

        runnable = ScanRunnable(requests)
        runnable.signals.finished.connect(self._on_scan_finished)
        # Keep the runnable (and therefore its signals object) alive until
        # _on_scan_finished runs -- see _active_scan_runnable in __init__.
        self._active_scan_runnable = runnable
        QtCore.QThreadPool.globalInstance().start(runnable)

    # Runs on the GUI thread once the background read completes: feeds newly-arrived
    # samples (not just the newest) through the alarm evaluator, and redraws every
    # unpaused plot/VMM-overlay curve referencing each channel.
    def _on_scan_finished(self, results):
        self._scan_in_flight = False
        self._active_scan_runnable = None  # the scan is done; safe to let it be collected
        now = time.time()
        all_transitions = []

        for channel_id, result in results.items():
            channel = self.channels[channel_id]
            if result[0] == 'error':
                self.log(f"[{channel.label}] read failed: {result[1]}", level='ERROR')
                continue
            _, x_data, y_data = result

            last_seen = self._alarm_last_ts.get(channel_id)
            newest_ts = last_seen
            samples = []
            for seconds_ago, value in zip(x_data, y_data):
                ts = now + float(seconds_ago)
                if newest_ts is None or ts > newest_ts:
                    newest_ts = ts
                if last_seen is None or ts > last_seen:
                    samples.append((ts, float(value)))
            samples.sort(key=lambda s: s[0])

            transitions = self.alarm_evaluator.evaluate_channel(channel_id, channel.alarm, samples, now, channel.log_interval_s)
            for transition in transitions:
                level = 'ALARM' if transition.to_state in (AlarmState.ALARM, AlarmState.STALE) else 'INFO'
                self.log(transition.message, level=level)
            all_transitions += transitions
            if newest_ts is not None:
                self._alarm_last_ts[channel_id] = newest_ts

            # Alarm evaluation above gets every row the scan read; a plot gets only
            # the rows its window covers. Those differ whenever the read was raised
            # to the ALARM_LOOKBACK_ROWS floor -- see rows_to_fetch(). Trim before
            # decimating, so DECIMATION_CAP is spent on points that will be drawn.
            window_rows = rows_for_window(self.window_seconds, channel.log_interval_s)

            # Decimate once per channel (independent of which plot/offset uses it)
            # and reuse for every consumer of this channel's curve.
            dec_x, dec_y = decimate_min_max(tail(x_data, window_rows), tail(y_data, window_rows),
                                            max_points=DECIMATION_CAP)

            for tab, plot_id in self.channel_plots.get(channel_id, []):
                plot = tab.plots.get(plot_id)
                if plot is None or not plot.running:
                    continue
                idx = plot.channel_ids.index(channel_id)
                set_point_markers(plot.curves[idx], dec_y)
                plot.curves[idx].setData(x=dec_x, y=dec_y + float(plot.offsets[idx]))

            for tab in self.tab_objects.values():
                if isinstance(tab, VMMTab) and not tab.paused and channel_id in tab.curves:
                    set_point_markers(tab.curves[channel_id], dec_y)
                    tab.curves[channel_id].setData(x=dec_x, y=dec_y)

        # Interlocks act on the same transitions the banner and event log just got,
        # ahead of the visual refresh: if an over-pressure is going to shut the gas,
        # the command should already be on its way by the time the banner appears.
        self.control_dock.handle_alarm_transitions(all_transitions)
        # After the interlocks, deliberately: if this tick's data trips an over-pressure,
        # the fill must find the setpoint control already latched and abort, rather than
        # issuing one more command into a vessel that is over its limit.
        self.control_dock.service_fill_controls()

        self._refresh_note_markers(now)
        self._refresh_alarm_visuals(now)

    def _refresh_alarm_visuals(self, now):
        for channel_id, refs in self.channel_plots.items():
            channel = self.channels[channel_id]
            state = self.alarm_evaluator.state_for(channel_id)
            status = display_status(state)
            for tab, plot_id in refs:
                plot = tab.plots[plot_id]
                idx = plot.channel_ids.index(channel_id)
                self._style_value_label(plot.value_labels[idx], channel, state, status, plot.offsets[idx], now)

        for tab in self.tab_objects.values():
            if not isinstance(tab, LiveTab):
                continue  # only LiveTab owns Plot objects with a border to color
            for plot in tab.plots.values():
                any_alarm = any(self.alarm_evaluator.state_for(cid).state == AlarmState.ALARM for cid in plot.channel_ids)
                self._set_plot_alarm_border(plot, any_alarm)

        self.status_strip.refresh(now)
        self.alarm_banner.refresh(now)
        self._refresh_tab_badges()
        if self.overview_tab is not None:
            self.overview_tab.refresh_values(now)
        for tab in self.tab_objects.values():
            if isinstance(tab, VMMTab):
                tab.refresh(now)

    def _refresh_tab_badges(self):
        for tab_name, tab in self.tab_objects.items():
            alarming = tab.alarming_channel_ids(self.alarm_evaluator)
            index = self.tabs.indexOf(tab)
            if index < 0:
                continue
            # Zero-padded count, and only ever re-set when the text actually changes:
            # the badge grows as channels go stale, and every setTabText re-derives
            # the tab bar's width, which propagates into the whole window's layout.
            text = f"{tab_name} ●{len(alarming):02d}" if alarming else tab_name
            if self.tabs.tabText(index) != text:
                self.tabs.setTabText(index, text)

    # Select the tab containing a channel's plot and scroll that plot into view --
    # used by status-strip/overview tile clicks and the alarm banner's message.
    def jump_to_channel(self, channel_id):
        refs = self.channel_plots.get(channel_id, [])
        if refs:
            tab, plot_id = refs[0]
            self.tabs.setCurrentWidget(tab)
            tab.scroll_to_plot(plot_id)
            return
        # VMM channels have no LiveTab/Plot of their own -- VMMTab renders them as
        # tiles + an overlay curve instead, so channel_plots never has an entry for
        # them. Fall back to selecting whichever VMMTab actually owns this channel.
        for tab in self.tab_objects.values():
            if isinstance(tab, VMMTab) and channel_id in tab.channel_ids:
                self.tabs.setCurrentWidget(tab)
                return

    def jump_to_tab(self, tab_name):
        tab = self.tab_objects.get(tab_name)
        if tab is not None:
            self.tabs.setCurrentWidget(tab)

    # Declare which channels (plain channel ids or AggregateTile specs) appear in
    # the always-visible status strip, and in what order.
    def set_status_strip(self, tiles):
        self.status_strip.set_tiles(tiles)

    def _style_value_label(self, label, channel, state, status, offset, now):
        text, style = format_channel_value(channel, state, status, offset, now)
        label.setText(f"{channel.label}: {text}")
        apply_style(label, style)

    def _set_plot_alarm_border(self, plot, in_alarm):
        if in_alarm == plot.in_alarm_visual:
            return
        plot.in_alarm_visual = in_alarm
        if in_alarm:
            plot.plot_widget.setStyleSheet(f"border: 2px solid {ALARM_COLOR};")
            plot.plot_widget.setTitle(plot.title, color=ALARM_COLOR)
        else:
            plot.plot_widget.setStyleSheet("")
            plot.plot_widget.setTitle(plot.title)

    # Create a tab in the window to put plots and buttons in
    # Declared on the plotter, not on a tab: a bus is a piece of hardware topology,
    # not part of any tab's display. Declare it BEFORE the controls that attach to it
    # -- each of those validates the reference and raises on a bad one.
    def add_serial_bus(self, id, label, script, port):
        return self.control_dock.add_serial_bus_group(id=id, label=label, script=script, port=port)

    # Declared on the plotter, not on a tab: a fill drives one control from another
    # subsystem's measurement, so it belongs to the system rather than to a display.
    # Declare it AFTER the setpoint control and pressure channels it names.
    def add_fill_control(self, id, label, setpoint_control, pressure_channels,
                         pressure_units, max_pressure, default_fast_flow, default_slow_flow,
                         default_slow_at, default_target, pressure_decimals=1, confirm=True):
        return self.control_dock.add_fill_group(
            id=id, label=label, setpoint_control_id=setpoint_control,
            pressure_channel_ids=pressure_channels, pressure_units=pressure_units,
            max_pressure=max_pressure, pressure_decimals=pressure_decimals,
            default_fast_flow=default_fast_flow, default_slow_flow=default_slow_flow,
            default_slow_at=default_slow_at, default_target=default_target, confirm=confirm,
        )

    # Declared on the plotter, not on a tab: an interlock is a system-wide safety
    # rule, not part of any one tab's display. Declare it AFTER the setpoint control
    # and the trigger channels it names -- it validates every reference immediately
    # and raises rather than arming an interlock that could never fire.
    def add_interlock(self, id, label, trigger_channels, setpoint_control, safe_value, trip_on_stale=True):
        return self.control_dock.add_interlock_group(
            id=id, label=label, trigger_channel_ids=trigger_channels,
            setpoint_control_id=setpoint_control, safe_value=safe_value,
            trip_on_stale=trip_on_stale,
        )

    def create_tab(self, tab_name, plots_per_row):
        tab = LiveTab(plots_per_row, plotter=self)
        tab.tab_name = tab_name
        self.tab_objects[tab_name] = tab
        self.tabs.addTab(tab, tab_name)
        return tab

    # Dashboard tab: tiles (value + sparkline) grouped by each channel's
    # overview_group, no live plots. Call this before any other create_tab() so it
    # lands first in tab order, and after every add_channel() call it should reflect.
    def build_overview_tab(self, tab_name='Overview'):
        tab = OverviewTab(self)
        tab.tab_name = tab_name
        self.overview_tab = tab
        self.tab_objects[tab_name] = tab
        self.tabs.addTab(tab, tab_name)
        return tab

    # VMM Temperatures tab: a 4x4 tile grid (one per channel, with a checkbox) beside
    # one overlay plot showing every checked channel's curve. threshold=None derives
    # the single threshold line from the first channel with alarm.high configured.
    def build_vmm_tab(self, tab_name, channel_ids, threshold=None):
        tab = VMMTab(self, channel_ids, threshold=threshold)
        tab.tab_name = tab_name
        self.tab_objects[tab_name] = tab
        self.tabs.addTab(tab, tab_name)
        return tab

    # End all running logger subprocesses
    def cleanup(self):
        self._save_layout()
        self.control_dock.cleanup()
        self.event_log.close_file()

    # Show the window and start the event loop
    def run(self):
        # The event log is a fixed system tab, not something launch_GUI.py declares,
        # so it's added here (the last thing that happens before showing the window)
        # rather than in __init__ -- that guarantees it lands after every tab
        # launch_GUI.py created, without launch_GUI.py needing to know it exists.
        self.tab_objects['Event Terminal'] = self.event_log
        self.event_log.tab_name = 'Event Terminal'
        self.tabs.addTab(self.event_log, 'Event Terminal')

        # Plain .show() sizes the window from the widget tree's natural sizeHint,
        # which for a QScrollArea with setWidgetResizable(True) is computed from its
        # *content* (e.g. every Gas System plot laid out without scrolling) rather
        # than being capped to anything -- so on first launch the window can come up
        # larger than any actual screen, and content beyond the screen's edge is
        # simply cut off instead of being reachable by scrolling. Size to the
        # current screen's available geometry and start maximized instead, so the
        # window itself can never exceed "fullscreen"; anything that still doesn't
        # fit scrolls within its own tab/pane as designed.
        screen = self.app.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            self.main_window.resize(available.width(), available.height())
        self.main_window.showMaximized()
        sys.exit(self.app.exec())


class LiveTab(QtWidgets.QWidget):
    def __init__(self, plots_per_row, plotter):
        super().__init__()  # Call the constructor of the parent class (QWidget) to properly initialize the widget. This class is now a custom QTWidget

        self.plotter = plotter  # back-reference, needed to resolve channel ids
        self.tab_name = None    # set by LivePlotter.create_tab() right after construction

        # Create a scroll area
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        self.scroll_area = scroll  # kept for scroll_to_plot()

        # Container widget inside the scroll area: a vertical stack of either
        # ungrouped plots (in one flat grid) or collapsible QGroupBoxes (each with
        # its own sub-grid), added in the order add_plot() is called (§5.3).
        container = QtWidgets.QWidget()
        self.layout = QtWidgets.QVBoxLayout(container)

        scroll.setWidget(container)

        # Main layout for the tab is just the scroll area
        outer_layout = QtWidgets.QVBoxLayout()
        outer_layout.addWidget(scroll)
        self.setLayout(outer_layout)

        self.plots_per_row = plots_per_row
        self._ungrouped_grid = None      # built lazily -- only if an ungrouped plot is actually added
        self._ungrouped_count = 0
        self._groups = {}  # group name -> {'inner': QWidget, 'grid': QGridLayout, 'count': int}

        self.plots = {}  # plot_id -> Plot (identity/config/runtime all in one place)

    def _channel(self, channel_id):
        return self.plotter.channels[channel_id]

    # Distinct channels across this tab's plots currently in ALARM/STALE -- used for
    # the tab-label badge (§4.6.4).
    def alarming_channel_ids(self, evaluator):
        result = set()
        for plot in self.plots.values():
            for channel_id in plot.channel_ids:
                if display_status(evaluator.state_for(channel_id)) in (DisplayStatus.ALARM, DisplayStatus.STALE):
                    result.add(channel_id)
        return result

    @staticmethod
    def _threshold_line_values(alarm):
        if alarm is None:
            return []
        values = []
        if alarm.high is not None:
            values.append(alarm.high)
        if alarm.low is not None:
            values.append(alarm.low)
        if alarm.abs_high is not None:
            values.append(alarm.abs_high)
            values.append(-alarm.abs_high)
        return values

    # Add a new plot. Its header row carries one current-value readout per channel
    # (kept live by the alarm scanner, so it still reflects reality while the plot
    # itself is paused) plus a small pause toggle, replacing the old full-width
    # "Stop <title>" button.
    def add_plot(self, plot_id, title, channels, x_axis, y_axis, offsets, group=None):
        # x_axis and y_axis are tuples of (label, unit); channels is a list of
        # channel ids registered via add_channel. How much history is displayed is
        # governed by the global time-window selector (§7), not a per-plot setting.
        plot = Plot(
            plot_id=plot_id, title=title, channel_ids=list(channels), x_axis=x_axis, y_axis=y_axis,
            offsets=list(offsets), group=group,
        )

        container = QtWidgets.QVBoxLayout()

        header_row = QtWidgets.QHBoxLayout()
        for channel_id in plot.channel_ids:
            value_label = QtWidgets.QLabel(f"{self._channel(channel_id).label}: —")
            value_label.setWordWrap(True)  # wrap instead of visually clipping when a multi-channel plot's combined text is wide
            header_row.addWidget(value_label)
            plot.value_labels.append(value_label)
        header_row.addStretch(1)
        # Follow/frozen and pause both live in this row and must not be confusable:
        # the indicator is a word ("FOLLOWING"/"FROZEN"), the pause toggle is a glyph
        # (⏸/▶), and they mean different things -- a frozen plot still receives data,
        # a paused one doesn't, and neither affects alarm evaluation.
        follow_button = make_follow_indicator(lambda p=plot_id: self.resume_live(p))
        follow_button.setFixedWidth(FOLLOW_INDICATOR_WIDTH)
        plot.follow_button = follow_button
        header_row.addWidget(follow_button)
        pause_button = QtWidgets.QPushButton("⏸")
        pause_button.setFixedWidth(28)
        pause_button.setToolTip(f"Pause {title}")
        pause_button.clicked.connect(lambda _, p=plot_id: self.toggle_plot(p))
        plot.pause_button = pause_button
        header_row.addWidget(pause_button)
        container.addLayout(header_row)

        # Create the plot widget
        plot_widget = pg.PlotWidget(title=title)
        plot_widget.setLabel('bottom', x_axis[0], units=x_axis[1])
        plot_widget.setLabel('left', y_axis[0], units=y_axis[1])
        plot_widget.showGrid(x=True, y=True)
        plot.plot_widget = plot_widget

        # sigStateChanged covers every route into and out of frozen: a user zoom/pan
        # (which turns autorange off), the auto-scale button, and the context menu's
        # Auto checkboxes. It also fires for the range changes autorange itself makes
        # as data is pushed in, which is harmless here only because the handler
        # re-reads the ViewBox's autorange flags rather than treating the signal as
        # meaning "the user did something" -- during a normal redraw those flags stay
        # on, so a scan tick can't read as a freeze.
        plot_widget.getViewBox().sigStateChanged.connect(
            lambda _vb, p=plot_id: self._on_view_state_changed(p))

        # Color mapping lives in a legend, not in the plot title, whenever a plot
        # overlays more than one channel.
        multi_curve = len(plot.channel_ids) > 1
        if multi_curve:
            plot_widget.addLegend()

        for i, channel_id in enumerate(plot.channel_ids):
            channel = self._channel(channel_id)
            color = COLOR_CYCLE[i % len(COLOR_CYCLE)]
            curve = plot_widget.plot(pen=color, name=channel.label if multi_curve else None)
            style_point_markers(curve, color)
            plot.curves.append(curve)

            # Threshold lines are drawn offset-corrected so they still line up
            # visually with the (offset-corrected) trace, even though alarm
            # evaluation itself always uses the raw, un-offset value.
            offset = plot.offsets[i]
            for line_value in self._threshold_line_values(channel.alarm):
                line = pg.InfiniteLine(pos=line_value + offset, angle=0, pen=pg.mkPen(ALARM_COLOR, style=QtCore.Qt.DashLine))
                plot_widget.addItem(line)
                plot.threshold_lines.append(line)

            self.plotter.channel_plots.setdefault(channel_id, []).append((self, plot_id))

        self.plots[plot_id] = plot
        plot.running = True  # plots redraw by default; LivePlotter's scan tick drives all of them centrally

        container.addWidget(plot_widget)

        # Wrap the layout in a QWidget and place it in its group's sub-grid (or the
        # tab's flat grid if ungrouped) -- see _grid_for_group().
        container_widget = QtWidgets.QWidget()
        container_widget.setLayout(container)
        container_widget.setMinimumSize(320, 300)
        grid, index = self._grid_for_group(group)
        row, col = index // self.plots_per_row, index % self.plots_per_row
        grid.addWidget(container_widget, row, col)
        plot.container_widget = container_widget

        return plot

    # Groups plots into collapsible QGroupBoxes (§5.3) stacked in the order first
    # seen; an ungrouped plot goes into one shared flat grid instead. Returns the
    # QGridLayout to place the next widget in, plus that grid's next free index.
    def _grid_for_group(self, group):
        if group is None:
            if self._ungrouped_grid is None:
                self._ungrouped_grid = QtWidgets.QGridLayout()
                self.layout.addLayout(self._ungrouped_grid)
            index = self._ungrouped_count
            self._ungrouped_count += 1
            return self._ungrouped_grid, index

        state = self._groups.get(group)
        if state is None:
            box = QtWidgets.QGroupBox(group)
            box.setCheckable(True)

            settings_key = f'group_collapsed/{self.tab_name}/{group}'
            collapsed = self.plotter.settings.value(settings_key, False, type=bool)
            box.setChecked(not collapsed)

            inner = QtWidgets.QWidget()
            grid = QtWidgets.QGridLayout()
            inner.setLayout(grid)
            inner.setVisible(not collapsed)

            box_layout = QtWidgets.QVBoxLayout()
            box_layout.addWidget(inner)
            box.setLayout(box_layout)

            def on_toggled(checked, inner=inner, key=settings_key):
                inner.setVisible(checked)
                self.plotter.settings.setValue(key, not checked)
            box.toggled.connect(on_toggled)

            self.layout.addWidget(box)
            state = {'grid': grid, 'count': 0}
            self._groups[group] = state

        index = state['count']
        state['count'] += 1
        return state['grid'], index

    # Scroll a specific plot into view within this tab's scroll area -- used when a
    # status-strip tile or the alarm banner's message is clicked.
    def scroll_to_plot(self, plot_id):
        plot = self.plots.get(plot_id)
        if plot is not None and plot.container_widget is not None:
            self.scroll_area.ensureWidgetVisible(plot.container_widget)

    # Toggle between running and paused for a given plot. This is just a flag
    # LivePlotter's scan tick checks before pushing new data into this plot's
    # curve(s) -- the channel's alarm evaluation is driven entirely by the
    # independent scan timer and keeps running regardless either way (§4.4).
    def toggle_plot(self, plot_id):
        plot = self.plots[plot_id]
        plot.running = not plot.running
        plot.pause_button.setText("⏸" if plot.running else "▶")
        plot.pause_button.setToolTip(f"{'Pause' if plot.running else 'Resume'} {plot.title}")
        # A paused plot isn't showing live data either, so the indicator says FROZEN
        # -- see set_follow_indicator().
        self._refresh_follow_indicator(plot)

    def _refresh_follow_indicator(self, plot):
        set_follow_indicator(plot.follow_button, following=plot.following, paused=not plot.running)

    # The ViewBox's range state changed. Re-derive whether this plot is still
    # tracking new data (see is_following) instead of assuming the change was a
    # freeze: this same signal fires for the auto-scale button and the context
    # menu's Auto checkboxes, which resume tracking, and for autorange's own updates
    # during a redraw, which change nothing. Runs often, so it does nothing at all
    # unless the answer actually changed.
    #
    # Either way the plot keeps receiving data and its channels keep being evaluated
    # for alarms -- only the view is frozen.
    def _on_view_state_changed(self, plot_id):
        plot = self.plots.get(plot_id)
        if plot is None:
            return  # fires during add_plot(), before the Plot is registered
        following = is_following(plot.plot_widget)
        if following != plot.following:
            plot.following = following
            self._refresh_follow_indicator(plot)

    # Re-enable autorange so the view snaps back to the live time window. Safe to
    # call on a plot that's already following -- Resume Following (All) and the
    # time-window dropdown both do exactly that. Deliberately does NOT unpause:
    # that's Resume All's job, and the two stay independent.
    def resume_following(self, plot_id):
        plot = self.plots[plot_id]
        resume_following_on(plot.plot_widget)
        plot.following = True
        self._refresh_follow_indicator(plot)

    def resume_following_all(self):
        for plot_id in self.plots:
            self.resume_following(plot_id)

    # What clicking the indicator itself does: undo whatever is keeping this plot off
    # live data. It has to unpause as well as re-follow, or clicking the amber badge
    # on a paused plot would be a dead affordance -- re-enabling an autorange that
    # has no new data to track looks like nothing happened.
    def resume_live(self, plot_id):
        if not self.plots[plot_id].running:
            self.toggle_plot(plot_id)
        self.resume_following(plot_id)

    # Register a logger's controls (LED, port, interval, start/stop) in the shared
    # control dock -- see ControlDock.add_logger_group for what this actually builds.
    def add_logger_control(self, id, label, log_filepath, interval_options, default_interval,
                           script=None, port=None, extra_args=None, bus=None,
                           unit_id=None, unit_type=None):
        return self.plotter.control_dock.add_logger_group(
            id=id, label=label, script=script, log_filepath=log_filepath, port=port,
            interval_options=interval_options, default_interval=default_interval,
            extra_args=extra_args, bus_id=bus, unit_id=unit_id, unit_type=unit_type,
        )

    # Register an Alicat MFC setpoint control (LED, port, value box, Set) in the same
    # control dock -- see ControlDock.add_setpoint_group for what this builds and how
    # it differs from a logger.
    def add_setpoint_control(self, id, label, unit_id, units, min_value, max_value,
                             script=None, port=None, decimals=2, default_value=0.0,
                             confirm=True, bus=None):
        return self.plotter.control_dock.add_setpoint_group(
            id=id, label=label, script=script, unit_id=unit_id, port=port, units=units,
            min_value=min_value, max_value=max_value, decimals=decimals,
            default_value=default_value, confirm=confirm, bus_id=bus,
        )

class ControlDock(QtWidgets.QWidget):
    '''Persistent right-hand dock, visible regardless of which tab is selected. Owns
    every logger's structured subprocess lifecycle (LED, port/interval dropdowns,
    start/stop, crash reporting) -- LiveTab.add_logger_control() just forwards here --
    and every MFC setpoint control's one-shot command subprocess
    (LiveTab.add_setpoint_control()).'''

    # stderr is read on a background thread (see _read_stderr); a signal is the only
    # safe way to hand a line back to the GUI thread for logging/widget updates --
    # Qt widgets must never be touched directly from a non-GUI thread.
    stderr_line_received = QtCore.pyqtSignal(str, str)  # logger_id, line
    # Same reasoning for setpoint commands, which are run to completion on a worker
    # thread (see _run_setpoint) rather than polled like a logger.
    setpoint_result_received = QtCore.pyqtSignal(str, int, str, str)  # setpoint_id, exit code, stdout, stderr
    # A shared bus reports on stdout (protocol events) and stderr (crashes); both are
    # read on background threads, so both come back through signals.
    bus_event_received = QtCore.pyqtSignal(str, str)   # bus_id, one JSON line
    bus_stderr_received = QtCore.pyqtSignal(str, str)  # bus_id, line

    def __init__(self, plotter):
        super().__init__()
        self.plotter = plotter
        self.loggers = {}  # id -> LoggerControl
        self.setpoints = {}  # id -> SetpointControl
        self.interlocks = {}  # id -> Interlock
        self.fills = {}  # id -> FillControl
        self.buses = {}  # id -> SerialBus

        self.layout = QtWidgets.QVBoxLayout()
        self.setLayout(self.layout)

        # Pinned above the scroll area: a red LED is only useful if it can be seen,
        # and once the instrument boxes scroll, a faulted one can be off screen. This
        # does for the dock what the alarm banner does for channels -- names what is
        # wrong, and scrolls to it when clicked. Hidden whenever nothing is wrong.
        self.fault_summary = QtWidgets.QLabel('')
        self.fault_summary.setWordWrap(True)
        self.fault_summary.setCursor(QtCore.Qt.PointingHandCursor)
        self.fault_summary.mousePressEvent = self._on_fault_summary_clicked
        self.fault_summary.hide()
        self.layout.addWidget(self.fault_summary)

        # The instrument boxes scroll; the global controls below them do not. Together
        # they wanted ~1256px of height with a ~1208px MINIMUM, and a Qt layout minimum
        # overrides the maximized window state -- so on anything short of a 1440p
        # screen the dock forced the whole window taller than the display. Only the
        # boxes move into the scroll area: the window selector, pause/resume and the
        # note input are used constantly and must never scroll away, and they are small
        # enough (~194px) to stay pinned. Two columns was the alternative and doesn't
        # fit -- the dock is only ~320-384px wide at the default splitter ratio, so the
        # fault lines would wrap to eight-plus lines and a faulted box would end up
        # TALLER than a healthy one.
        self._scroll = QtWidgets.QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        # With the horizontal bar off, the content's minimum WIDTH still propagates out
        # to the dock, so the splitter can't collapse it narrower than a Start button
        # -- while the height, the dimension that was overflowing, is free to scroll.
        # Leaving it on AsNeeded would let the dock shrink to nothing and scroll
        # sideways, which for a column of fixed-width buttons is just worse.
        self._scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        instruments = QtWidgets.QWidget()
        instruments_layout = QtWidgets.QVBoxLayout()
        instruments_layout.setContentsMargins(0, 0, 0, 0)
        instruments.setLayout(instruments_layout)

        # Each section is a real widget, not a bare nested QVBoxLayout. A layout added
        # to another layout does not reliably push a size change up the chain when a
        # widget is added to it later, so the scroll content reported a height of 0 and
        # the scroll area concluded everything fit -- no scrollbar, content clipped. A
        # widget's sizeHint does propagate, via updateGeometry, which is what makes the
        # scroll area notice the content grew.
        def section():
            holder = QtWidgets.QWidget()
            holder_layout = QtWidgets.QVBoxLayout()
            holder_layout.setContentsMargins(0, 0, 0, 0)
            holder.setLayout(holder_layout)
            instruments_layout.addWidget(holder)
            return holder_layout

        # Buses and loggers share one layout, filled in declaration order, so a bus
        # sits directly above the controls that attach to it rather than in a block of
        # its own -- grouping by widget type instead put unrelated instruments between
        # a bus and the controls it serves, which is precisely what it has to explain.
        self.loggers_layout = section()

        # Its own layout, below the loggers: reading and commanding are different
        # kinds of action, and a Set button must never sit in the same visual block
        # as a Start/Stop that only affects what gets recorded.
        self.setpoints_layout = section()

        # Between the setpoint control it drives and the interlock that can overrule
        # it -- the order the three act in when a fill runs into an over-pressure.
        self.fills_layout = section()

        # Below the control each one latches, so the trip state and the locked Set
        # button read as one thing.
        self.interlocks_layout = section()

        instruments_layout.addStretch(1)
        self._scroll.setWidget(instruments)
        # The only stretchy thing in the dock, so every pixel the pinned rows don't
        # need goes to showing instruments.
        self.layout.addWidget(self._scroll, 1)

        window_row = QtWidgets.QHBoxLayout()
        window_row.addWidget(QtWidgets.QLabel('Window:'))
        self.window_combo = QtWidgets.QComboBox()
        for option_label, seconds in WINDOW_OPTIONS:
            self.window_combo.addItem(option_label, userData=seconds)
        default_index = self.window_combo.findData(DEFAULT_WINDOW_S)
        self.window_combo.setCurrentIndex(default_index if default_index >= 0 else 0)
        saved_window = self.plotter.settings.value('window_seconds')
        if saved_window is not None:
            saved_index = self.window_combo.findData(int(saved_window))
            if saved_index >= 0:
                self.window_combo.setCurrentIndex(saved_index)
        self.plotter.window_seconds = self.window_combo.currentData()
        self.window_combo.currentIndexChanged.connect(self._on_window_changed)
        window_row.addWidget(self.window_combo)
        self.layout.addLayout(window_row)

        pause_row = QtWidgets.QHBoxLayout()
        pause_all_button = QtWidgets.QPushButton('Pause All')
        pause_all_button.clicked.connect(self._pause_all)
        pause_row.addWidget(pause_all_button)
        resume_all_button = QtWidgets.QPushButton('Resume All')
        resume_all_button.clicked.connect(self._resume_all)
        pause_row.addWidget(resume_all_button)
        self.layout.addLayout(pause_row)

        # Separate from Pause/Resume All on purpose: pause governs whether a plot
        # receives data at all, following governs whether its view scrolls to show
        # the data it receives. Its own row so the two aren't read as one group.
        follow_row = QtWidgets.QHBoxLayout()
        resume_following_button = QtWidgets.QPushButton('Resume Following (All)')
        resume_following_button.setToolTip('Snap every plot back to the live time window')
        resume_following_button.clicked.connect(self.resume_following_all)
        follow_row.addWidget(resume_following_button)
        self.layout.addLayout(follow_row)

        # In the dock, not in the Event Terminal tab: the operator has to be able to
        # jot a note without leaving whatever tab they're watching.
        note_row = QtWidgets.QHBoxLayout()
        note_row.addWidget(QtWidgets.QLabel('Note:'))
        self.note_input = QtWidgets.QLineEdit()
        self.note_input.setPlaceholderText('Type a note, press Enter')
        self.note_input.setToolTip('Logs a NOTE line, appends to operator_notes.csv, '
                                   'and marks every plot at this moment')
        self.note_input.returnPressed.connect(self._on_note_entered)
        note_row.addWidget(self.note_input)
        self.layout.addLayout(note_row)

        self.stderr_line_received.connect(self._on_stderr_line)
        self.bus_event_received.connect(self._on_bus_event)
        self.bus_stderr_received.connect(self._on_bus_stderr)
        self.setpoint_result_received.connect(self._on_setpoint_result)

        # Polls every logger's subprocess for an unexpected exit; this is deliberately
        # decoupled from any single logger's own start/stop so a crash is caught even
        # if nothing else touches that logger's controls.
        self._status_timer = QtCore.QTimer()
        self._status_timer.timeout.connect(self._poll_loggers)
        # Same timer, separate concern: retries a tripped interlock's unconfirmed
        # command and keeps its Reset button in step with the live alarm states.
        self._status_timer.timeout.connect(self._service_interlocks)
        self._status_timer.timeout.connect(self._refresh_fault_summary)
        self._status_timer.start(500)

    # Everything currently wrong, in the order it appears in the dock, as
    # [(what, its group box)]. Recomputed from state each tick rather than maintained
    # incrementally: a dozen call sites setting a flag is a dozen chances to leave the
    # summary claiming all-clear over a red LED.
    def _current_faults(self):
        faults = []
        for bus in self.buses.values():
            if bus.port_fault is not None:
                faults.append((f'{bus.label} port', bus.box))
        for logger in self.loggers.values():
            if logger.faulted:
                faults.append((logger.label, logger.box))
        for fill in self.fills.values():
            if fill.stage == 'aborted':
                faults.append((f'{fill.label} ABORTED', fill.box))
        for interlock in self.interlocks.values():
            if interlock.tripped:
                faults.append((f'{interlock.label} TRIPPED', interlock.box))
        return faults

    def _refresh_fault_summary(self):
        faults = self._current_faults()
        if not faults:
            self.fault_summary.hide()
            return
        names = ', '.join(name for name, _ in faults)
        self.fault_summary.setStyleSheet(
            f'color: white; background-color: {ALARM_COLOR}; font-weight: bold; '
            f'font-size: 10px; padding: 4px; border-radius: 3px;')
        self.fault_summary.setText(f'⚠ {names} — click to show')
        self.fault_summary.show()

    def _on_fault_summary_clicked(self, event):
        faults = self._current_faults()
        if faults and faults[0][1] is not None:
            self._scroll.ensureWidgetVisible(faults[0][1])

    def _on_note_entered(self):
        if self.plotter.add_operator_note(self.note_input.text()) is not None:
            self.note_input.clear()  # left intact if the note was blank, so nothing is lost

    def _set_led(self, logger, state):
        logger.led.setStyleSheet(f"background-color: {LED_COLORS[state]}; border-radius: 6px;")

    def _on_window_changed(self, idx):
        seconds = self.window_combo.itemData(idx)
        self.plotter.window_seconds = seconds
        self.plotter.settings.setValue('window_seconds', seconds)
        # Asking for "1h" and having plots stay frozen at some older range would be
        # baffling -- choosing a window is a statement about what you want to see.
        self.resume_following_all()

    # Re-enable range tracking on every plot in every tab, including the VMM overlay.
    # Unrelated to pause: a frozen plot was already receiving data.
    def resume_following_all(self):
        for tab in self.plotter.tab_objects.values():
            if isinstance(tab, LiveTab):
                tab.resume_following_all()
            elif isinstance(tab, VMMTab):
                tab.resume_following()

    # Pauses/resumes every plot's own curve-redraw timer across every tab -- alarm
    # evaluation is unaffected either way (§4.4), same as pausing one plot at a time.
    def _pause_all(self):
        for tab in self.plotter.tab_objects.values():
            if isinstance(tab, LiveTab):
                for plot in tab.plots.values():
                    if plot.running:
                        tab.toggle_plot(plot.plot_id)
            elif isinstance(tab, VMMTab):
                tab.pause()

    def _resume_all(self):
        for tab in self.plotter.tab_objects.values():
            if isinstance(tab, LiveTab):
                for plot in tab.plots.values():
                    if not plot.running:
                        tab.toggle_plot(plot.plot_id)
            elif isinstance(tab, VMMTab):
                tab.resume()

    # The declared port is always offered even when it isn't currently enumerated --
    # a USB adapter that's unplugged (or a device that's off) at launch must not make
    # its port unselectable once it comes back.
    @staticmethod
    def _make_port_combo(port):
        combo = QtWidgets.QComboBox()
        available_ports = [p.device for p in serial.tools.list_ports.comports()]
        if port not in available_ports:
            available_ports = [port] + available_ports
        combo.addItems(available_ports)
        combo.setCurrentText(port)
        return combo

    # Build one shared bus's group box: LED, the port dropdown (which lives here, not
    # on the controls -- there is one physical port and showing three copies of it
    # invited exactly the confusion this class exists to remove), and a line naming
    # what currently holds it open.
    def add_serial_bus_group(self, id, label, script, port):
        bus = SerialBus(id=id, label=label, script=script, port=port)

        box = QtWidgets.QGroupBox(f'{label} (shared port)')
        box_layout = QtWidgets.QVBoxLayout()
        box.setLayout(box_layout)

        status_row = QtWidgets.QHBoxLayout()
        led = QtWidgets.QLabel()
        led.setFixedSize(12, 12)
        bus.led = led
        status_row.addWidget(led)
        status_row.addWidget(QtWidgets.QLabel(label))
        status_row.addStretch(1)
        box_layout.addLayout(status_row)

        port_combo = self._make_port_combo(port)
        # Changing the port under a running bus would silently leave every attached
        # control talking to the old one, so it is locked while anything holds it.
        port_combo.currentTextChanged.connect(lambda _, bid=id: self._refresh_bus(self.buses[bid]))
        bus.port_combo = port_combo
        box_layout.addWidget(port_combo)

        status_label = QtWidgets.QLabel('')
        status_label.setStyleSheet('font-size: 10px; color: #7f8c8d;')
        status_label.setWordWrap(True)
        bus.status_label = status_label
        box_layout.addWidget(status_label)

        bus.box = box
        self.buses[id] = bus
        self._set_led(bus, 'stopped')
        self._refresh_bus(bus)
        self.loggers_layout.addWidget(box)
        return bus

    def _refresh_bus(self, bus):
        running = bus.process is not None and bus.process.poll() is None
        bus.port_combo.setEnabled(not running)
        # Checked before "is it running": a fault normally stops every control on the
        # bus and so closes it, and a box reading a placid "Closed" would hide the
        # reason the controls next to it just went red.
        if bus.port_fault is not None:
            self._set_led(bus, 'crashed')
            bus.status_label.setStyleSheet(f'color: {ALARM_COLOR}; font-weight: bold; font-size: 10px;')
            retry = 'Retrying.' if running else 'Start a control to try again.'
            # The driver's message usually ends in a period of its own.
            bus.status_label.setText(f'CANNOT OPEN {bus.port_combo.currentText()} — '
                                     f'{bus.port_fault.rstrip(". ")}. {retry}')
            return
        if not running:
            self._set_led(bus, 'stopped')
            bus.status_label.setStyleSheet('font-size: 10px; color: #7f8c8d;')
            bus.status_label.setText('Closed — opens automatically when a control below needs it')
            return
        names = sorted(self._bus_user_label(bus, user) for user in bus.users)
        self._set_led(bus, 'running')
        bus.status_label.setStyleSheet('font-size: 10px; color: #2ecc71;')
        bus.status_label.setText(f'Open on {bus.port_combo.currentText()} — '
                                 f'{", ".join(names) if names else "closing"}')

    def _bus_user_label(self, bus, user_id):
        base = user_id.split(':', 1)[0]
        if base in self.loggers:
            return self.loggers[base].label
        if base in self.setpoints:
            return f'{self.setpoints[base].label} (command)'
        return base

    # Opening the port is a side effect of something needing it, never an explicit
    # operator action -- which is what makes "open on first use, close on last" hold
    # without anyone having to remember to do either.
    def _acquire_bus(self, bus, user_id):
        if bus.process is None or bus.process.poll() is not None:
            if not self._start_bus(bus):
                return False
        bus.users.add(user_id)
        self._refresh_bus(bus)
        return True

    def _release_bus(self, bus, user_id):
        bus.users.discard(user_id)
        if not bus.users:
            self._stop_bus(bus)
        self._refresh_bus(bus)

    def _start_bus(self, bus):
        port = bus.port_combo.currentText()
        argv = [sys.executable, bus.script, port]
        try:
            process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True, bufsize=1)
        except OSError as e:
            self.plotter.log(f"[{bus.label}] could not start bus: {e}", level='ERROR')
            return False

        bus.process = process
        bus.ready = False
        bus.port_fault = None  # a fresh attempt starts from a clean slate
        bus.user_stopped = False
        bus.stderr_lines = []

        # stdout carries the protocol, stderr carries crashes; both are read off the
        # GUI thread and handed back by signal.
        threading.Thread(target=self._read_bus_stdout, args=(bus,), daemon=True).start()
        threading.Thread(target=self._read_bus_stderr, args=(bus,), daemon=True).start()

        self.plotter.log(f"[{bus.label}] opened {port}", level='INFO')
        return True

    def _stop_bus(self, bus):
        if bus.process is None:
            return
        bus.user_stopped = True
        try:
            if bus.process.poll() is None:
                bus.process.stdin.write(json.dumps({'cmd': 'quit'}) + '\n')
                bus.process.stdin.flush()
                bus.process.wait(timeout=3)  # let it close the port cleanly
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
        self._kill(bus.process)
        bus.process = None
        bus.ready = False
        self.plotter.log(f"[{bus.label}] closed {bus.port_combo.currentText()}", level='INFO')

    def _send_bus(self, bus, command):
        if bus.process is None or bus.process.poll() is not None:
            return False
        try:
            bus.process.stdin.write(json.dumps(command) + '\n')
            bus.process.stdin.flush()
            return True
        except (OSError, ValueError) as e:
            self.plotter.log(f"[{bus.label}] command failed: {e}", level='ERROR')
            return False

    def _read_bus_stdout(self, bus):
        # Background thread -- must not touch Qt widgets. Emitting marshals to the GUI.
        stream = bus.process.stdout
        if stream is None:
            return
        for line in stream:
            line = line.strip()
            if line:
                self.bus_event_received.emit(bus.id, line)

    def _read_bus_stderr(self, bus):
        stream = bus.process.stderr
        if stream is None:
            return
        for line in stream:
            line = line.rstrip('\n')
            if line:
                self.bus_stderr_received.emit(bus.id, line)

    def _on_bus_stderr(self, bus_id, line):
        bus = self.buses.get(bus_id)
        if bus is None:
            return
        bus.stderr_lines.append(line)
        if len(bus.stderr_lines) > 200:
            bus.stderr_lines.pop(0)
        self.plotter.log(f"[{bus.label}] {line}", level='ERROR')

    def _on_bus_event(self, bus_id, raw):
        bus = self.buses.get(bus_id)
        if bus is None:
            return
        try:
            event = json.loads(raw)
        except ValueError:
            self.plotter.log(f"[{bus.label}] unparseable event: {raw}", level='ERROR')
            return

        kind = event.get('event')
        if kind == 'ready':
            bus.ready = True
            self._refresh_bus(bus)
        elif kind == 'set_result':
            self._on_bus_set_result(bus, event)
        elif kind == 'polled':
            self._on_bus_unit_polled(bus, event.get('unit_id'), event.get('row'))
        elif kind == 'poll_error':
            # Not fatal -- one dropped or mis-addressed frame is skipped rather than
            # written, and the next poll tries again. Logged so a bus that is dropping
            # frames steadily is visible rather than silently thinning the data, and
            # counted so a unit that never answers stops looking healthy.
            self.plotter.log(f"[{bus.label}] unit {event.get('unit_id')}: "
                             f"{event.get('detail')}", level='ERROR')
            self._on_bus_unit_poll_error(bus, event.get('unit_id'), event.get('detail', ''))
        elif kind == 'port_error':
            self._on_bus_port_fault(bus, event.get('detail', 'port unavailable'))
        elif kind == 'port_ok':
            self._on_bus_port_ok(bus)
        elif kind == 'error':
            self.plotter.log(f"[{bus.label}] {event.get('detail')}", level='ERROR')

    # A port fault is shown at once, with no debounce: the bus process being alive
    # says nothing about the port, so this is the only thing standing between the
    # operator and a green LED over a port that cannot be opened.
    def _on_bus_port_fault(self, bus, detail):
        if bus.port_fault == detail:
            return
        bus.port_fault = detail
        self.plotter.log(f"[{bus.label}] {bus.port_combo.currentText()} unavailable: {detail}",
                         level='ERROR')
        for logger in list(self.loggers.values()):
            if logger.bus_id == bus.id and logger.running:
                self._fault_bus_logger(logger, f'{bus.label}: {detail}')
        self._refresh_bus(bus)

    def _on_bus_port_ok(self, bus):
        if bus.port_fault is None:
            return
        bus.port_fault = None
        self.plotter.log(f"[{bus.label}] {bus.port_combo.currentText()} recovered", level='INFO')
        self._refresh_bus(bus)

    def _on_bus_unit_polled(self, bus, unit_id, row=None):
        logger = self._bus_logger_for(bus, unit_id)
        if logger is None or not logger.running:
            return
        logger.poll_errors = 0
        logger.last_poll_error = ''
        # Echoed to the console, exactly where log_pressure.py's and
        # log_H2O_readings.py's own prints end up: those run as subprocesses whose
        # stdout is inherited, so their output lands in the terminal the GUI was
        # launched from, not in the Event Terminal. Deliberately NOT the event log --
        # two units at 2s is a line every second, which would bury alarms and logger
        # crashes. flush=True because stdout is block-buffered when it isn't a tty.
        if row:
            print(f'Alicat {logger.label} (unit {unit_id}): {row}', flush=True)

    def _on_bus_unit_poll_error(self, bus, unit_id, detail):
        logger = self._bus_logger_for(bus, unit_id)
        if logger is None or not logger.running:
            return
        logger.poll_errors += 1
        logger.last_poll_error = detail
        if logger.poll_errors >= BUS_POLL_ERRORS_BEFORE_FAULT:
            self._fault_bus_logger(logger, f'{logger.poll_errors} bad frames in a row: {detail}')

    def _bus_logger_for(self, bus, unit_id):
        for logger in self.loggers.values():
            if logger.bus_id == bus.id and logger.unit_id == unit_id:
                return logger
        return None

    # Ends a bus logger the same way _poll_loggers ends one whose subprocess died:
    # red LED, the reason on its group box, and the button back to Start. Leaving it
    # "running" with a Stop button while it is plainly not logging is the half-state
    # that made a broken bus look like a working one -- a control either is running or
    # it is not, and the button has to say which.
    def _fault_bus_logger(self, logger, detail):
        if not logger.running:
            return
        bus = self.buses[logger.bus_id]
        self._send_bus(bus, {'cmd': 'stop', 'unit_id': logger.unit_id})
        logger.running = False
        logger.user_stopped = False
        logger.poll_errors = 0
        logger.last_poll_error = ''
        logger.faulted = True
        logger.start_stop_button.setText(f'Start {logger.label}')
        logger.start_stop_button.setStyleSheet("background-color: green;")
        self._set_led(logger, 'crashed')
        logger.error_label.setText(detail)
        logger.error_label.show()
        self.plotter.log(f"Logger {logger.label} stopped: {detail}", level='ERROR')
        # Releasing can close the port outright if this was the last thing holding it,
        # which is the point: a failed start leaves nothing behind.
        self._release_bus(bus, logger.id)

    # A bus setpoint reply is funnelled into exactly the same result path a one-shot
    # subprocess takes, so the dock, the event log and the interlock cannot end up
    # treating the two transports differently.
    def _on_bus_set_result(self, bus, event):
        for control in self.setpoints.values():
            if control.bus_id == bus.id and control.unit_id == event.get('unit_id') and control.sending:
                detail = event.get('detail', '')
                ok = bool(event.get('ok'))
                self._release_bus(bus, f'{control.id}:command')
                self._on_setpoint_result(control.id, 0 if ok else 1,
                                         detail if ok else '', '' if ok else detail)
                return

    # Build one logger's group box: LED, port dropdown (from the live serial port
    # list, defaulting to the declared port), interval dropdown, start/stop button,
    # and a hidden error line that appears only on an unexpected exit.
    def add_logger_group(self, id, label, log_filepath, interval_options, default_interval,
                         script=None, port=None, extra_args=None, bus_id=None,
                         unit_id=None, unit_type=None):
        _check_transport('logger', id, bus_id, self.buses, script, port)
        if bus_id is not None and (unit_id is None or unit_type is None):
            raise ValueError(f"logger {id!r}: a bus logger needs unit_id and unit_type "
                             f"-- they are how the bus knows which instrument to poll")
        logger = LoggerControl(
            id=id, label=label, script=script, log_filepath=log_filepath,
            interval_options=interval_options, default_interval=default_interval, port=port,
            extra_args=[str(a) for a in (extra_args or [])],
            bus_id=bus_id, unit_id=unit_id, unit_type=unit_type,
        )

        box = QtWidgets.QGroupBox(label)
        box_layout = QtWidgets.QVBoxLayout()
        box.setLayout(box_layout)

        status_row = QtWidgets.QHBoxLayout()
        led = QtWidgets.QLabel()
        led.setFixedSize(12, 12)
        logger.led = led
        status_row.addWidget(led)
        status_row.addWidget(QtWidgets.QLabel(label))
        status_row.addStretch(1)
        box_layout.addLayout(status_row)

        # A bus logger has no port of its own -- the bus owns the port, and a second
        # dropdown here could only ever disagree with it.
        if bus_id is None:
            port_combo = self._make_port_combo(port)
            logger.port_combo = port_combo
            box_layout.addWidget(port_combo)

        interval_combo = QtWidgets.QComboBox()
        default_index = 0
        for i, (option_label, option_value) in enumerate(interval_options):
            interval_combo.addItem(option_label, userData=option_value)
            if option_value == default_interval:
                default_index = i
        interval_combo.setCurrentIndex(default_index)
        interval_combo.currentIndexChanged.connect(lambda idx, lid=id: self._interval_changed(lid, idx))
        logger.interval_combo = interval_combo
        box_layout.addWidget(interval_combo)

        error_label = QtWidgets.QLabel('')
        error_label.setStyleSheet('color: #e74c3c; font-size: 10px;')
        error_label.setWordWrap(True)
        error_label.hide()
        logger.error_label = error_label
        box_layout.addWidget(error_label)

        start_stop_button = QtWidgets.QPushButton(f'Start {label}')
        start_stop_button.setStyleSheet("background-color: green;")
        start_stop_button.clicked.connect(lambda _, lid=id: self._toggle_logger(lid))
        logger.start_stop_button = start_stop_button
        box_layout.addWidget(start_stop_button)

        logger.box = box
        self.loggers[id] = logger
        # The logger control is the authority on its channels' rate -- it is what
        # writes the file. Reconciling here means a launch_GUI.py that declares
        # log_interval_s=2 on a channel and default_interval=10 on its logger can't
        # leave the two quietly disagreeing before the first start.
        self._apply_log_interval(logger, default_interval)
        self._set_led(logger, 'stopped')
        self.loggers_layout.addWidget(box)
        return logger

    # A channel's log_interval_s drives two things: when the alarm evaluator calls it
    # STALE (stale_multiplier x interval) and how many rows a plot fetches for the
    # time window. Both are statements about how often data ACTUALLY arrives, so both
    # have to follow the rate the logger is really running at -- not the one declared
    # in launch_GUI.py. Without this, switching the dropdown to 1m left the channel
    # believing 2s, so every reading arrived ~50s after it had already been called
    # stale, and the plot drew 30x more history than the window claimed.
    def _apply_log_interval(self, logger, interval):
        changed = []
        for channel_id in channels_fed_by(self.plotter.channels, logger.log_filepath):
            channel = self.plotter.channels[channel_id]
            if channel.log_interval_s != interval:
                channel.log_interval_s = interval
                changed.append(channel.label)
        if changed:
            multiplier = AlarmSpec().stale_multiplier
            self.plotter.log(f"[{logger.label}] now logging every {interval}s -- "
                             f"{', '.join(changed)} go stale after {multiplier * interval:g}s",
                             level='INFO')

    # Changing the interval while a logger is running must not silently do nothing:
    # stash it and apply on the next start, rather than restarting the process here.
    def _interval_changed(self, logger_id, idx):
        logger = self.loggers[logger_id]
        new_value = logger.interval_combo.itemData(idx)
        if logger.running:
            logger.pending_interval = new_value
            self.plotter.log(f"[{logger.label}] interval change to {logger.interval_combo.itemText(idx)} will apply on next start", level='INFO')
        else:
            logger.pending_interval = None

    def _toggle_logger(self, logger_id):
        logger = self.loggers[logger_id]
        if logger.running:
            self._stop_logger(logger)
        else:
            self._start_logger(logger)

    def _start_logger(self, logger):
        interval = logger.pending_interval if logger.pending_interval is not None else logger.interval_combo.currentData()
        if logger.bus_id is not None:
            self._start_bus_logger(logger, interval)
            return
        port = logger.port_combo.currentText()
        # argv is a real list -- never a shell string -- so filenames/ports with
        # spaces need no special handling, and sys.executable ensures the venv
        # interpreter (not a bare 'python' off PATH) runs the logger.
        # extra_args sits between the port and the interval, which is where a script
        # that needs more than <log_filepath> <port> <interval> takes them (an Alicat
        # needs its unit id and unit type there). A real argv list, so an argument
        # containing a space -- 'Sensor Only' -- arrives as one argument.
        argv = [sys.executable, logger.script, logger.log_filepath, port,
                *logger.extra_args, str(interval)]

        process = subprocess.Popen(argv, stderr=subprocess.PIPE, text=True, bufsize=1)
        logger.process = process
        logger.user_stopped = False
        logger.stderr_lines = []
        logger.pending_interval = None

        # Read stderr on a background thread so the GUI thread never blocks on it;
        # captured lines are surfaced on an unexpected exit (see _poll_loggers).
        thread = threading.Thread(target=self._read_stderr, args=(logger,), daemon=True)
        thread.start()

        logger.running = True
        logger.faulted = False
        self._apply_log_interval(logger, interval)
        logger.start_stop_button.setText(f'Stop {logger.label}')
        logger.start_stop_button.setStyleSheet("background-color: red;")
        logger.error_label.hide()
        self._set_led(logger, 'running')
        extras = f", {' '.join(logger.extra_args)}" if logger.extra_args else ''
        self.plotter.log(f"Started logger: {logger.label} ({port}{extras}, {interval}s)", level='INFO')

    # A bus logger has no subprocess of its own: it acquires the shared port and
    # becomes one polled unit on it. Everything the operator sees -- LED, button,
    # event log -- is identical to a standalone logger's, because from the dock's
    # point of view the only difference is what carries the readings.
    def _start_bus_logger(self, logger, interval):
        bus = self.buses[logger.bus_id]
        if not self._acquire_bus(bus, logger.id):
            logger.error_label.setText('could not open the shared port')
            logger.error_label.show()
            self._set_led(logger, 'crashed')
            return
        sent = self._send_bus(bus, {
            'cmd': 'poll', 'unit_id': logger.unit_id, 'unit_type': logger.unit_type,
            'interval_s': interval, 'log_filepath': logger.log_filepath,
        })
        if not sent:
            self._release_bus(bus, logger.id)
            logger.error_label.setText('the shared port did not accept the command')
            logger.error_label.show()
            self._set_led(logger, 'crashed')
            return

        logger.user_stopped = False
        logger.pending_interval = None
        logger.poll_errors = 0
        logger.last_poll_error = ''
        logger.faulted = False
        logger.running = True
        self._apply_log_interval(logger, interval)
        logger.start_stop_button.setText(f'Stop {logger.label}')
        logger.start_stop_button.setStyleSheet("background-color: red;")
        logger.error_label.hide()
        self._set_led(logger, 'running')
        self.plotter.log(f"Started logger: {logger.label} "
                         f"({bus.port_combo.currentText()}, unit {logger.unit_id}, {interval}s)", level='INFO')

    def _read_stderr(self, logger):
        # Runs on a background thread -- must not touch Qt widgets or self.plotter
        # directly. Emitting a signal marshals the call onto the GUI thread.
        process = logger.process
        if process.stderr is None:
            return
        for line in process.stderr:
            line = line.rstrip('\n')
            if line:
                self.stderr_line_received.emit(logger.id, line)

    def _on_stderr_line(self, logger_id, line):
        logger = self.loggers.get(logger_id)
        if logger is None:
            return
        logger.stderr_lines.append(line)
        if len(logger.stderr_lines) > 200:
            logger.stderr_lines.pop(0)
        self.plotter.log(f"[{logger.label}] {line}", level='ERROR')

    def _stop_logger(self, logger):
        logger.user_stopped = True
        if logger.bus_id is not None:
            bus = self.buses[logger.bus_id]
            self._send_bus(bus, {'cmd': 'stop', 'unit_id': logger.unit_id})
            self._release_bus(bus, logger.id)
        else:
            self._kill(logger.process)
        logger.running = False
        logger.faulted = False
        logger.start_stop_button.setText(f'Start {logger.label}')
        logger.start_stop_button.setStyleSheet("background-color: green;")
        self._set_led(logger, 'stopped')
        self.plotter.log(f"Stopped logger: {logger.label}", level='INFO')

    @staticmethod
    def _kill(process):
        if process and process.poll() is None:
            if platform.system() == 'Windows':
                subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], check=True)
            else:
                process.kill()

    # Never silently flip a crashed logger's button back to "everything is fine" --
    # an unexpected exit gets a red LED and the last captured stderr line.
    def _poll_loggers(self):
        for bus in self.buses.values():
            if bus.process is not None and bus.process.poll() is not None and not bus.user_stopped:
                self._on_bus_died(bus)
        for logger in self.loggers.values():
            if logger.bus_id is not None:
                continue  # its health is the bus's health -- see _on_bus_died
            if logger.running and logger.process is not None and logger.process.poll() is not None:
                exit_code = logger.process.returncode
                logger.running = False
                logger.start_stop_button.setText(f'Start {logger.label}')
                logger.start_stop_button.setStyleSheet("background-color: green;")
                if logger.user_stopped:
                    self._set_led(logger, 'stopped')
                else:
                    logger.faulted = True
                    self._set_led(logger, 'crashed')
                    last_line = logger.stderr_lines[-1] if logger.stderr_lines else '(no stderr captured)'
                    logger.error_label.setText(f'exit code {exit_code}: {last_line}')
                    logger.error_label.show()
                    self.plotter.log(f"Logger {logger.label} exited unexpectedly (code {exit_code}): {last_line}", level='ERROR')

    # Build one MFC setpoint control's group box: LED, port dropdown, a bounded value
    # box in the controller's engineering units, and a Set button. There is no
    # start/stop and no interval: a setpoint is one command, not a process. The LED
    # reports the last command's outcome rather than a running/stopped state.
    def add_setpoint_group(self, id, label, unit_id, units, min_value, max_value,
                           decimals, default_value, confirm, script=None, port=None, bus_id=None):
        _check_transport('setpoint control', id, bus_id, self.buses, script, port)
        control = SetpointControl(
            id=id, label=label, script=script, unit_id=unit_id, port=port, units=units,
            min_value=min_value, max_value=max_value, decimals=decimals,
            default_value=default_value, confirm=confirm, bus_id=bus_id,
        )

        box = QtWidgets.QGroupBox(f'{label} Setpoint')
        box_layout = QtWidgets.QVBoxLayout()
        box.setLayout(box_layout)

        status_row = QtWidgets.QHBoxLayout()
        led = QtWidgets.QLabel()
        led.setFixedSize(12, 12)
        control.led = led
        status_row.addWidget(led)
        status_row.addWidget(QtWidgets.QLabel(label))
        status_row.addStretch(1)
        box_layout.addLayout(status_row)

        # As with a bus logger: the bus owns the port, so there is no second dropdown
        # here that could disagree with it.
        if bus_id is None:
            port_combo = self._make_port_combo(port)
            control.port_combo = port_combo
            box_layout.addWidget(port_combo)

        value_row = QtWidgets.QHBoxLayout()
        # A spin box with a hard range, not a free-text field: the controller accepts
        # whatever it is sent, so the range declared in launch_GUI.py (the device's
        # full scale) is the one place a fat-fingered 500 for 50 gets caught. It has
        # to be clamped before the command is built, never validated after the fact.
        value_spinbox = QtWidgets.QDoubleSpinBox()
        value_spinbox.setDecimals(decimals)
        value_spinbox.setRange(min_value, max_value)
        value_spinbox.setValue(default_value)
        value_spinbox.setToolTip(f'{min_value:g} to {max_value:g} {units} '
                                 f'(the controller\'s configured full scale)')
        control.value_spinbox = value_spinbox
        value_row.addWidget(value_spinbox)
        value_row.addWidget(QtWidgets.QLabel(units))
        box_layout.addLayout(value_row)

        # Hidden until a command has actually been sent -- an empty line here would
        # read as "no reply", which is a different thing from "nothing asked yet".
        status_label = QtWidgets.QLabel('')
        status_label.setStyleSheet('font-size: 10px;')
        status_label.setWordWrap(True)
        status_label.hide()
        control.status_label = status_label
        box_layout.addWidget(status_label)

        send_button = QtWidgets.QPushButton(f'Set {label}')
        send_button.clicked.connect(lambda _, sid=id: self._send_setpoint(sid))
        control.send_button = send_button
        box_layout.addWidget(send_button)

        control.box = box
        self.setpoints[id] = control
        self._set_led(control, 'stopped')
        self.setpoints_layout.addWidget(box)
        return control

    # The operator path: validate, confirm, then hand off to the shared dispatcher.
    # An interlock's safe-value command deliberately does NOT come through here -- it
    # must not be blocked by the lock it just applied, and it must not sit waiting on
    # a modal dialog.
    def _send_setpoint(self, setpoint_id):
        control = self.setpoints[setpoint_id]
        if control.sending:
            return  # button is disabled while in flight; this is the belt to that braces

        # Refusing here as well as disabling the button: the lock is a safety state,
        # so it can't rest on a widget's enabled flag alone.
        if control.locked_by is not None:
            interlock = self.interlocks.get(control.locked_by)
            name = interlock.label if interlock is not None else control.locked_by
            self.plotter.log(f"[{control.label}] setpoint refused: {name} is tripped "
                             f"and must be reset first", level='ERROR')
            return

        value = control.value_spinbox.value()
        port = self._control_port(control)

        # Unlike starting a logger, this moves gas. Confirming names the value, the
        # units, the port and the unit id, so the operator is checking the actual
        # command rather than re-reading the number they just typed.
        if control.confirm:
            answer = QtWidgets.QMessageBox.question(
                self, f'Set {control.label} Setpoint',
                f'Set {control.label} (unit {control.unit_id}) to '
                f'{value:g} {control.units} on {port}?',
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.Cancel,
                QtWidgets.QMessageBox.Cancel)
            if answer != QtWidgets.QMessageBox.Yes:
                self.plotter.log(f"[{control.label}] setpoint {value:g} {control.units} cancelled", level='INFO')
                return

        self._dispatch_setpoint(control, value, source='operator')

    # Actually fire one command. Shared by the operator's Set button and by an
    # interlock's safe-value command; `source` is what the result handler uses to
    # tell them apart. Returns False if the port is already busy, which is the
    # interlock's cue to try again on its next tick rather than to give up.
    # Where this control's commands actually go -- its own dropdown, or the bus's.
    def _control_port(self, control):
        if control.bus_id is not None:
            return self.buses[control.bus_id].port_combo.currentText()
        return control.port_combo.currentText()

    def _dispatch_setpoint(self, control, value, source):
        if control.sending:
            return False

        port = self._control_port(control)
        if control.bus_id is not None:
            return self._dispatch_setpoint_over_bus(control, value, source, port)
        # Same argv discipline as a logger (a real list, sys.executable), but a
        # different argument order -- an Alicat control script takes
        # <serial_port> <unit_id> <setpoint>, with no log file and no interval.
        argv = [sys.executable, control.script, port, control.unit_id, f'{value:g}']

        control.sending = True
        control.last_sent_value = value
        control.command_source = source
        control.send_button.setEnabled(False)
        control.send_button.setText('Sending...')
        control.status_label.hide()
        self._set_led(control, 'sending')
        via = '' if source == 'operator' else f' [{source}]'
        self.plotter.log(f"[{control.label}] sending setpoint {value:g} {control.units} "
                         f"({port}, unit {control.unit_id}){via}", level='INFO')

        thread = threading.Thread(target=self._run_setpoint, args=(control.id, argv), daemon=True)
        thread.start()
        return True

    # A setpoint over a shared bus holds the port only for as long as the command is
    # in flight, so a Set pressed with no logger running still opens the port, sends,
    # and closes it again -- and one pressed while a logger is running just rides the
    # connection that logger already holds.
    def _dispatch_setpoint_over_bus(self, control, value, source, port):
        bus = self.buses[control.bus_id]
        user = f'{control.id}:command'
        if not self._acquire_bus(bus, user):
            self._on_setpoint_result(control.id, 1, '', f'could not open {port}')
            return False
        if not self._send_bus(bus, {'cmd': 'set', 'unit_id': control.unit_id, 'value': value}):
            self._release_bus(bus, user)
            self._on_setpoint_result(control.id, 1, '', f'{port} did not accept the command')
            return False

        control.sending = True
        control.last_sent_value = value
        control.command_source = source
        control.command_token += 1
        control.send_button.setEnabled(False)
        control.send_button.setText('Sending...')
        control.status_label.hide()
        self._set_led(control, 'sending')
        via = '' if source == 'operator' else f' [{source}]'
        self.plotter.log(f"[{control.label}] sending setpoint {value:g} {control.units} "
                         f"({port}, unit {control.unit_id}){via}", level='INFO')

        # A one-shot subprocess is bounded by subprocess.run's own timeout; a bus
        # command is not, so it needs one here. Without it a bus that never answers
        # would leave the button disabled forever -- and a tripped interlock waiting
        # on a reply that never comes would never retry.
        QtCore.QTimer.singleShot(
            int(SETPOINT_TIMEOUT_S * 1000),
            lambda cid=control.id, token=control.command_token: self._bus_setpoint_timeout(cid, token))
        return True

    def _bus_setpoint_timeout(self, control_id, token):
        control = self.setpoints.get(control_id)
        # The token guards against a timeout for an earlier command firing on a later
        # one that has since taken its place.
        if control is None or not control.sending or control.command_token != token:
            return
        bus = self.buses.get(control.bus_id)
        if bus is not None:
            self._release_bus(bus, f'{control.id}:command')
        self._on_setpoint_result(control_id, -1, '', f'no reply within {SETPOINT_TIMEOUT_S}s')

    def _run_setpoint(self, setpoint_id, argv):
        # Runs on a background thread -- must not touch Qt widgets or self.plotter
        # directly. The command opens a serial port and waits on the device, so
        # running it on the GUI thread would freeze the plots for seconds; emitting
        # the result marshals it back onto the GUI thread.
        try:
            completed = subprocess.run(argv, capture_output=True, text=True, timeout=SETPOINT_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            self.setpoint_result_received.emit(setpoint_id, -1, '', f'no reply within {SETPOINT_TIMEOUT_S}s')
            return
        except OSError as e:
            self.setpoint_result_received.emit(setpoint_id, -1, '', str(e))
            return
        self.setpoint_result_received.emit(setpoint_id, completed.returncode,
                                           completed.stdout or '', completed.stderr or '')

    def _on_setpoint_result(self, setpoint_id, exit_code, stdout, stderr):
        control = self.setpoints.get(setpoint_id)
        if control is None:
            return

        control.sending = False
        source = control.command_source
        control.command_source = None
        control.send_button.setText(f'Set {control.label}')
        # A latched control stays disabled whatever this command's outcome was --
        # including the operator command that happened to be in flight when the
        # interlock tripped.
        control.send_button.setEnabled(control.locked_by is None)

        reply = last_line(stdout)
        error = last_line(stderr)

        acknowledged = setpoint_acknowledged(exit_code, reply)

        value = control.last_sent_value
        value_text = f'{value:g} {control.units}' if value is not None else f'? {control.units}'
        if acknowledged:
            self._set_led(control, 'running')
            control.status_label.setStyleSheet('color: #2ecc71; font-size: 10px;')
            control.status_label.setText(f'{value_text} acknowledged at {time.strftime("%H:%M:%S")}')
            self.plotter.log(f"[{control.label}] setpoint {value_text} acknowledged", level='INFO')
        else:
            detail = reply or error or f'exit code {exit_code}, no output'
            self._set_led(control, 'crashed')
            control.status_label.setStyleSheet(f'color: {ALARM_COLOR}; font-size: 10px;')
            control.status_label.setText(f'{value_text} FAILED: {detail}')
            # A setpoint that didn't take is exactly the kind of thing that must not
            # be silently swallowed -- the controller may still be at its old value.
            self.plotter.log(f"[{control.label}] setpoint {value_text} FAILED: {detail}", level='ERROR')
        control.status_label.show()

        if source is not None and source != 'operator':
            detail = reply or error or f'exit code {exit_code}'
            if source in self.fills:
                self._on_fill_command_result(source, acknowledged, value, detail)
            else:
                self._on_interlock_command_result(source, acknowledged, detail)

    # Build one fill control's group box: the four numbers that define the fill, an
    # Engage/Stop button, and a status line. Sits below the setpoint control it drives
    # and above the interlock that can overrule it, which is the order they act in.
    def add_fill_group(self, id, label, setpoint_control_id, pressure_channel_ids,
                       pressure_units, max_pressure, pressure_decimals,
                       default_fast_flow, default_slow_flow, default_slow_at,
                       default_target, confirm):
        if setpoint_control_id not in self.setpoints:
            raise ValueError(f"fill control {id!r}: no setpoint control {setpoint_control_id!r} "
                             f"(declare it first; known: {sorted(self.setpoints)})")
        unknown = [c for c in pressure_channel_ids if c not in self.plotter.channels]
        if unknown:
            raise ValueError(f"fill control {id!r}: unknown pressure channel(s) {unknown} "
                             f"(known: {sorted(self.plotter.channels)})")
        if not pressure_channel_ids:
            raise ValueError(f"fill control {id!r}: needs at least one pressure channel -- "
                             f"it is the only thing that tells the fill when to stop")

        control = self.setpoints[setpoint_control_id]
        fill = FillControl(
            id=id, label=label, setpoint_control_id=setpoint_control_id,
            pressure_channel_ids=list(pressure_channel_ids), pressure_units=pressure_units,
            max_pressure=max_pressure, pressure_decimals=pressure_decimals,
            default_fast_flow=default_fast_flow, default_slow_flow=default_slow_flow,
            default_slow_at=default_slow_at, default_target=default_target, confirm=confirm,
        )

        box = QtWidgets.QGroupBox(f'{label} (auto fill)')
        box_layout = QtWidgets.QVBoxLayout()
        box.setLayout(box_layout)

        status_row = QtWidgets.QHBoxLayout()
        led = QtWidgets.QLabel()
        led.setFixedSize(12, 12)
        fill.led = led
        status_row.addWidget(led)
        status_row.addWidget(QtWidgets.QLabel(label))
        status_row.addStretch(1)
        box_layout.addLayout(status_row)

        # Laid out in the order the fill runs -- fast, then the handover, then slow,
        # then the stop -- so reading the box top to bottom describes the sequence.
        grid = QtWidgets.QGridLayout()

        def row(n, text, spinbox):
            grid.addWidget(QtWidgets.QLabel(text), n, 0)
            grid.addWidget(spinbox, n, 1)

        def flow_box(value):
            spin = QtWidgets.QDoubleSpinBox()
            spin.setDecimals(control.decimals)
            spin.setRange(control.min_value, control.max_value)
            spin.setValue(value)
            spin.setSuffix(f' {control.units}')
            return spin

        def pressure_box(value):
            spin = QtWidgets.QDoubleSpinBox()
            spin.setDecimals(pressure_decimals)
            spin.setRange(0.0, max_pressure)
            spin.setValue(value)
            spin.setSuffix(f' {pressure_units}')
            return spin

        fill.fast_flow_spinbox = flow_box(default_fast_flow)
        fill.slow_at_spinbox = pressure_box(default_slow_at)
        fill.slow_flow_spinbox = flow_box(default_slow_flow)
        fill.target_spinbox = pressure_box(default_target)
        row(0, 'Fast rate', fill.fast_flow_spinbox)
        row(1, 'Slow down at', fill.slow_at_spinbox)
        row(2, 'Slow rate', fill.slow_flow_spinbox)
        row(3, 'Stop at', fill.target_spinbox)
        box_layout.addLayout(grid)

        status_label = QtWidgets.QLabel('')
        status_label.setStyleSheet('font-size: 10px;')
        status_label.setWordWrap(True)
        fill.status_label = status_label
        box_layout.addWidget(status_label)

        engage_button = QtWidgets.QPushButton(f'Engage {label}')
        engage_button.clicked.connect(lambda _, fid=id: self._toggle_fill(fid))
        fill.engage_button = engage_button
        box_layout.addWidget(engage_button)

        fill.box = box
        self.fills[id] = fill
        self._refresh_fill(fill)
        self.fills_layout.addWidget(box)
        return fill

    # The highest reading among the pressure channels that are currently trustworthy.
    # Highest, because two gauges disagreeing during a fill is a reason to believe the
    # one saying you are closer to the target. Channels that are not OK are skipped
    # entirely -- a switched-off low-range gauge is normal and must not veto the fill,
    # but a stale or alarming one contributes nothing either. None means no usable
    # reading at all, which is the one condition a running fill cannot survive.
    def _fill_pressure(self, fill):
        readings = []
        for channel_id in fill.pressure_channel_ids:
            state = self.plotter.alarm_evaluator.state_for(channel_id)
            if (state.state == AlarmState.OK and state.last_timestamp is not None
                    and state.last_value is not None and not math.isnan(state.last_value)):
                readings.append(state.last_value)
        return max(readings) if readings else None

    # The lowest over-pressure alarm limit among the channels this fill steers by --
    # the ceiling a target must stay below, since crossing it trips the interlock.
    def _fill_alarm_limit(self, fill):
        limits = [self.plotter.channels[c].alarm.high
                  for c in fill.pressure_channel_ids
                  if self.plotter.channels[c].alarm is not None
                  and self.plotter.channels[c].alarm.high is not None]
        return min(limits) if limits else None

    def _toggle_fill(self, fill_id):
        fill = self.fills[fill_id]
        if fill.engaged:
            self._stop_fill(fill, 'stopped by the operator')
        else:
            self._engage_fill(fill)

    def _engage_fill(self, fill):
        control = self.setpoints[fill.setpoint_control_id]
        if control.locked_by is not None:
            interlock = self.interlocks.get(control.locked_by)
            name = interlock.label if interlock is not None else control.locked_by
            self._set_fill_status(fill, f'cannot start: {name} is tripped', ok=False)
            self.plotter.log(f"[{fill.label}] refused: {name} is tripped", level='ERROR')
            return

        fast = fill.fast_flow_spinbox.value()
        slow = fill.slow_flow_spinbox.value()
        slow_at = fill.slow_at_spinbox.value()
        target = fill.target_spinbox.value()
        pressure = self._fill_pressure(fill)

        problem = fill_settings_error(fast, slow, slow_at, target, pressure,
                                      self._fill_alarm_limit(fill))
        if problem:
            self._set_fill_status(fill, f'cannot start: {problem}', ok=False)
            self.plotter.log(f"[{fill.label}] refused: {problem}", level='ERROR')
            return

        if fill.confirm:
            answer = QtWidgets.QMessageBox.question(
                self, f'Engage {fill.label}',
                f'Fill at {fast:g} {control.units} until {slow_at:g} {fill.pressure_units}, '
                f'then {slow:g} {control.units} until {target:g} {fill.pressure_units}?\n\n'
                f'{control.label} is at {pressure:g} {fill.pressure_units} now. '
                f'This opens gas automatically.',
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.Cancel,
                QtWidgets.QMessageBox.Cancel)
            if answer != QtWidgets.QMessageBox.Yes:
                self.plotter.log(f"[{fill.label}] engage cancelled", level='INFO')
                return

        fill.engaged = True
        fill.stage = 'fast'
        fill.desired_flow = fast
        fill.commanded_flow = None  # nothing acknowledged for this fill yet
        fill.failures = 0
        fill.engage_button.setText(f'STOP {fill.label}')
        fill.engage_button.setStyleSheet("background-color: red;")
        for spin in (fill.fast_flow_spinbox, fill.slow_flow_spinbox,
                     fill.slow_at_spinbox, fill.target_spinbox):
            spin.setEnabled(False)  # the fill is running to these numbers; changing them mid-fill would be ambiguous
        self.plotter.log(f"[{fill.label}] ENGAGED: {fast:g} {control.units} to "
                         f"{slow_at:g} {fill.pressure_units}, then {slow:g} {control.units} "
                         f"to {target:g} {fill.pressure_units} (now {pressure:g})", level='ALARM')
        self._service_fill(fill)
        self._refresh_fill(fill)

    def _stop_fill(self, fill, reason, stage='idle'):
        fill.engaged = False
        fill.stage = stage
        fill.desired_flow = 0.0
        fill.detail = reason
        fill.engage_button.setText(f'Engage {fill.label}')
        fill.engage_button.setStyleSheet('')
        for spin in (fill.fast_flow_spinbox, fill.slow_flow_spinbox,
                     fill.slow_at_spinbox, fill.target_spinbox):
            spin.setEnabled(True)
        level = 'ALARM' if stage == 'aborted' else 'INFO'
        self.plotter.log(f"[{fill.label}] {reason} -- closing to 0 {self.setpoints[fill.setpoint_control_id].units}",
                         level=level)
        self._push_fill_flow(fill)  # get the valve shut before anything else happens
        self._refresh_fill(fill)

    # Called once per scan tick, when fresh pressure has just arrived.
    def service_fill_controls(self):
        for fill in self.fills.values():
            if fill.engaged:
                self._service_fill(fill)
            elif fill.desired_flow and fill.commanded_flow != fill.desired_flow:
                self._push_fill_flow(fill)   # a stop whose command hasn't landed yet
            self._refresh_fill(fill)

    def _service_fill(self, fill):
        control = self.setpoints[fill.setpoint_control_id]

        # The interlock outranks the fill; if it has latched the control, this fill is
        # over. Checked first so a trip can't be papered over by the next command.
        if control.locked_by is not None:
            interlock = self.interlocks.get(control.locked_by)
            name = interlock.label if interlock is not None else control.locked_by
            self._stop_fill(fill, f'ABORTED: {name} tripped', stage='aborted')
            return

        pressure = self._fill_pressure(fill)
        if pressure is None:
            # Filling on a pressure nobody can read is the thing this must never do.
            self._stop_fill(fill, 'ABORTED: no usable pressure reading', stage='aborted')
            return

        stage = fill_stage_for(pressure, fill.slow_at_spinbox.value(), fill.target_spinbox.value())
        if stage == 'done':
            fill.stage = 'done'
            self._stop_fill(fill, f'target reached at {pressure:g} {fill.pressure_units}', stage='done')
            return

        if stage != fill.stage:
            self.plotter.log(f"[{fill.label}] {pressure:g} {fill.pressure_units} -- "
                             f"{fill.stage} -> {stage} rate", level='INFO')
            fill.stage = stage
        fill.desired_flow = fill_flow_for(stage, fill.fast_flow_spinbox.value(),
                                          fill.slow_flow_spinbox.value(), fill.desired_flow)
        self._push_fill_flow(fill)

    # Closes the gap between what the fill wants and what the MFC last acknowledged.
    # Re-sending on the next tick is the whole retry mechanism: a command lost to a
    # busy port or a one-off error simply goes again a second later.
    def _push_fill_flow(self, fill):
        control = self.setpoints[fill.setpoint_control_id]
        if fill.commanded_flow == fill.desired_flow or control.sending:
            return
        if control.locked_by is not None and fill.desired_flow:
            return  # locked and we want flow: the interlock check will end this fill
        if fill.failures >= INTERLOCK_MAX_ATTEMPTS:
            return
        self._dispatch_setpoint(control, fill.desired_flow, source=fill.id)

    def _on_fill_command_result(self, fill_id, acknowledged, value, detail):
        fill = self.fills.get(fill_id)
        if fill is None:
            return
        if acknowledged:
            fill.commanded_flow = value
            fill.failures = 0
            # The MFC is at this value now, so the setpoint box has to read it too --
            # the operator isn't typing in that box during an automatic fill, and
            # leaving it on a stale number would misstate the hardware. Same reasoning
            # as the interlock syncing it to the safe value on a trip.
            self.setpoints[fill.setpoint_control_id].value_spinbox.setValue(value)
        else:
            fill.failures += 1
            self.plotter.log(f"[{fill.label}] attempt {fill.failures} of {INTERLOCK_MAX_ATTEMPTS} "
                             f"to set {value:g} failed ({detail})", level='ALARM')
            if fill.failures >= INTERLOCK_MAX_ATTEMPTS:
                if fill.engaged:
                    self._stop_fill(fill, 'ABORTED: cannot command the MFC', stage='aborted')
                else:
                    self.plotter.log(f"[{fill.label}] COULD NOT CLOSE THE MFC after "
                                     f"{INTERLOCK_MAX_ATTEMPTS} attempts -- SHUT THE GAS MANUALLY",
                                     level='ALARM')
        self._refresh_fill(fill)

    def _set_fill_status(self, fill, text, ok=True):
        color = '#2ecc71' if ok else ALARM_COLOR
        weight = '' if ok else 'font-weight: bold; '
        fill.status_label.setStyleSheet(f'color: {color}; {weight}font-size: 10px;')
        fill.status_label.setText(text)

    def _refresh_fill(self, fill):
        control = self.setpoints[fill.setpoint_control_id]
        pressure = self._fill_pressure(fill)
        reading = f'{pressure:g} {fill.pressure_units}' if pressure is not None else 'no reading'

        if fill.engaged:
            self._set_led(fill, 'sending' if fill.stage == 'fast' else 'running')
            self._set_fill_status(
                fill, f'{fill.stage.upper()} at {fill.desired_flow:g} {control.units} — '
                      f'{reading}, stopping at {fill.target_spinbox.value():g} {fill.pressure_units}')
            return

        if fill.stage == 'aborted' or fill.failures >= INTERLOCK_MAX_ATTEMPTS:
            self._set_led(fill, 'crashed')
            self._set_fill_status(fill, fill.detail or 'aborted', ok=False)
        elif fill.stage == 'done':
            self._set_led(fill, 'running')
            self._set_fill_status(fill, fill.detail or 'target reached')
        else:
            self._set_led(fill, 'stopped')
            fill.status_label.setStyleSheet('font-size: 10px; color: #7f8c8d;')
            fill.status_label.setText(fill.detail or f'Idle — {reading}')

    # Build one interlock's group box: LED, what it watches and what it does, a live
    # status line, and a Reset button that stays disabled until a reset is actually
    # allowed. Deliberately the last block in the dock, under the control it latches.
    def add_interlock_group(self, id, label, trigger_channel_ids, setpoint_control_id,
                            safe_value, trip_on_stale):
        # Fail at startup, not silently at trip time. A typo in a channel id or a
        # control id would otherwise produce an interlock that looks armed in the GUI
        # and does nothing when the pressure actually rises -- the single worst
        # failure mode this feature has.
        if setpoint_control_id not in self.setpoints:
            raise ValueError(f"interlock {id!r}: no setpoint control {setpoint_control_id!r} "
                             f"(declare it before the interlock; known: {sorted(self.setpoints)})")
        unknown = [c for c in trigger_channel_ids if c not in self.plotter.channels]
        if unknown:
            raise ValueError(f"interlock {id!r}: unknown trigger channel(s) {unknown} "
                             f"(known: {sorted(self.plotter.channels)})")
        without_alarm = [c for c in trigger_channel_ids if self.plotter.channels[c].alarm is None]
        if without_alarm:
            raise ValueError(f"interlock {id!r}: trigger channel(s) {without_alarm} have no AlarmSpec, "
                             f"so they can never enter ALARM and the interlock could never trip")

        interlock = Interlock(
            id=id, label=label, trigger_channel_ids=list(trigger_channel_ids),
            setpoint_control_id=setpoint_control_id, safe_value=safe_value,
            trip_on_stale=trip_on_stale,
        )

        control = self.setpoints[setpoint_control_id]
        box = QtWidgets.QGroupBox(f'{label} (interlock)')
        box_layout = QtWidgets.QVBoxLayout()
        box.setLayout(box_layout)

        status_row = QtWidgets.QHBoxLayout()
        led = QtWidgets.QLabel()
        led.setFixedSize(12, 12)
        interlock.led = led
        status_row.addWidget(led)
        status_row.addWidget(QtWidgets.QLabel(label))
        status_row.addStretch(1)
        box_layout.addLayout(status_row)

        # Spelled out rather than left to the launch file: an operator looking at the
        # dock has to be able to see what this thing will do to the gas, and on what.
        triggers = ', '.join(self.plotter.channels[c].label for c in trigger_channel_ids)
        summary = QtWidgets.QLabel(f'{triggers} in alarm '
                                   f'{"or stale " if trip_on_stale else ""}'
                                   f'→ {control.label} to {safe_value:g} {control.units}')
        summary.setStyleSheet('font-size: 10px; color: #7f8c8d;')
        summary.setWordWrap(True)
        box_layout.addWidget(summary)

        status_label = QtWidgets.QLabel('')
        status_label.setStyleSheet('font-size: 10px;')
        status_label.setWordWrap(True)
        interlock.status_label = status_label
        box_layout.addWidget(status_label)

        reset_button = QtWidgets.QPushButton('Reset Interlock')
        reset_button.setToolTip('Re-enables the setpoint control. Allowed only once the '
                                'vessel pressure is readable again.')
        reset_button.clicked.connect(lambda _, iid=id: self._reset_interlock(iid))
        interlock.reset_button = reset_button
        box_layout.addWidget(reset_button)

        interlock.box = box
        self.interlocks[id] = interlock
        self._refresh_interlock(interlock)
        self.interlocks_layout.addWidget(box)
        return interlock

    # Called once per scan tick with every transition the alarm evaluator produced.
    # The interlock rides the alarm state machine rather than comparing values itself,
    # so it fires on exactly the event the operator sees in the banner.
    def handle_alarm_transitions(self, transitions):
        if not self.interlocks:
            return
        if not transitions:
            for interlock in self.interlocks.values():
                self._update_armed(interlock)
            return
        for interlock in self.interlocks.values():
            self._update_armed(interlock)
            if interlock.tripped or not interlock.armed:
                continue  # already tripped (a second trigger changes nothing), or not yet armed
            for transition in transitions:
                if interlock_should_trip(transition, interlock.trigger_channel_ids, interlock.trip_on_stale):
                    self._trip_interlock(interlock, transition.message)
                    break

    # Arms on the first tick where the vessel's pressure is readable. That is the same
    # predicate a reset uses -- "the pressure is knowable right now" is exactly what
    # makes it safe both to start guarding and to let flow resume.
    def _update_armed(self, interlock):
        if interlock.armed or self._blocking_channels(interlock):
            return
        interlock.armed = True
        self.plotter.log(f"INTERLOCK {interlock.label} armed", level='INFO')
        self._refresh_interlock(interlock)

    def _trip_interlock(self, interlock, reason):
        control = self.setpoints[interlock.setpoint_control_id]
        interlock.tripped = True
        interlock.trip_reason = reason
        interlock.trip_timestamp = time.time()
        interlock.command_confirmed = False
        interlock.attempts = 0
        interlock.next_attempt_time = None
        interlock.gave_up = False

        # Latch first, command second. If the command is slow (or the port is busy
        # with an operator's command) the control must already be refusing new flow
        # by the time anyone can click it.
        control.locked_by = interlock.id
        control.send_button.setEnabled(False)
        for fill in list(self.fills.values()):
            if fill.engaged and fill.setpoint_control_id == control.id:
                self._stop_fill(fill, f'ABORTED: {interlock.label} tripped', stage='aborted')
        # A greyed-out button with no explanation is its own failure mode -- the one
        # moment the operator most needs to know why they can't command flow.
        control.send_button.setToolTip(f'Locked: {interlock.label} interlock is tripped. '
                                       f'Reset it below to command flow again.')

        self.plotter.log(f"INTERLOCK {interlock.label} TRIPPED ({reason}) -- driving "
                         f"{control.label} to {interlock.safe_value:g} {control.units}", level='ALARM')
        self._attempt_interlock_command(interlock)
        self._refresh_interlock(interlock)

    def _attempt_interlock_command(self, interlock):
        control = self.setpoints[interlock.setpoint_control_id]
        if interlock.attempts >= INTERLOCK_MAX_ATTEMPTS:
            if not interlock.gave_up:
                interlock.gave_up = True
                self.plotter.log(f"INTERLOCK {interlock.label} COULD NOT SET {control.label} to "
                                 f"{interlock.safe_value:g} {control.units} after "
                                 f"{INTERLOCK_MAX_ATTEMPTS} attempts -- SHUT THE GAS MANUALLY",
                                 level='ALARM')
                self._refresh_interlock(interlock)
            return
        interlock.attempts += 1
        if not self._dispatch_setpoint(control, interlock.safe_value, source=interlock.id):
            # Port busy with another command -- not a failure, so don't spend the
            # attempt; back it out and let the next tick try again.
            interlock.attempts -= 1
            interlock.next_attempt_time = time.time() + 0.5

    def _on_interlock_command_result(self, interlock_id, acknowledged, detail):
        interlock = self.interlocks.get(interlock_id)
        if interlock is None:
            return
        control = self.setpoints[interlock.setpoint_control_id]
        if acknowledged:
            interlock.command_confirmed = True
            interlock.next_attempt_time = None
            # The controller is at the safe value now, so the box has to read it too --
            # leaving the operator's old number showing would misstate the hardware.
            interlock.gave_up = False
            control.value_spinbox.setValue(interlock.safe_value)
            self.plotter.log(f"INTERLOCK {interlock.label}: {control.label} confirmed at "
                             f"{interlock.safe_value:g} {control.units}", level='ALARM')
        else:
            self.plotter.log(f"INTERLOCK {interlock.label}: attempt {interlock.attempts} of "
                             f"{INTERLOCK_MAX_ATTEMPTS} to set {control.label} failed ({detail})",
                             level='ALARM')
            interlock.next_attempt_time = time.time() + INTERLOCK_RETRY_DELAY_S
        self._refresh_interlock(interlock)

    # Driven by the same 500ms timer that polls loggers: retries an unconfirmed safe
    # command and keeps each Reset button's enabled state in step with the live alarm
    # states (which change on scan ticks this dock never sees).
    def _service_interlocks(self):
        now = time.time()
        for interlock in self.interlocks.values():
            self._update_armed(interlock)
            if (interlock.tripped and not interlock.command_confirmed and not interlock.gave_up
                    and interlock.next_attempt_time is not None and now >= interlock.next_attempt_time):
                interlock.next_attempt_time = None
                self._attempt_interlock_command(interlock)
            self._refresh_interlock(interlock)

    # A reset is allowed once the vessel's pressure is readable again -- see
    # interlock_reset_blockers for exactly what that means, and why a switched-off
    # gauge must not count against it.
    def _blocking_channels(self, interlock):
        states = {channel_id: self.plotter.alarm_evaluator.state_for(channel_id)
                  for channel_id in interlock.trigger_channel_ids}
        return [(self.plotter.channels[channel_id].label, reason)
                for channel_id, reason in interlock_reset_blockers(states)]

    def _reset_interlock(self, interlock_id):
        interlock = self.interlocks[interlock_id]
        if not interlock.tripped:
            return
        blocking = self._blocking_channels(interlock)
        if blocking:
            detail = ', '.join(f'{label} {state}' for label, state in blocking)
            self.plotter.log(f"INTERLOCK {interlock.label}: reset refused, still {detail}", level='ERROR')
            self._refresh_interlock(interlock)
            return

        control = self.setpoints[interlock.setpoint_control_id]
        interlock.tripped = False
        interlock.command_confirmed = False
        interlock.gave_up = False
        interlock.attempts = 0
        interlock.next_attempt_time = None
        interlock.trip_reason = ''
        interlock.trip_timestamp = None

        control.locked_by = None
        control.send_button.setEnabled(not control.sending)
        control.send_button.setToolTip('')
        # Reset unlocks the control; it deliberately does NOT restore the setpoint
        # that was in force before the trip. Flow only ever resumes because someone
        # typed a value and pressed Set.
        self.plotter.log(f"INTERLOCK {interlock.label} reset -- {control.label} re-enabled "
                         f"(still at {interlock.safe_value:g} {control.units})", level='INFO')
        self._refresh_interlock(interlock)

    def _refresh_interlock(self, interlock):
        control = self.setpoints[interlock.setpoint_control_id]
        if not interlock.armed:
            # Honest about not guarding anything yet, rather than showing a reassuring
            # "Armed" while the pressure data it watches hasn't arrived.
            blocking = self._blocking_channels(interlock)
            waiting = ', '.join(f'{label} {state}' for label, state in blocking)
            self._set_led(interlock, 'stopped')
            interlock.status_label.setStyleSheet('color: #7f8c8d; font-size: 10px;')
            interlock.status_label.setText(f'Not armed — waiting for good data ({waiting}). '
                                           f'Arms automatically once a trigger channel reads OK.')
            interlock.reset_button.setEnabled(False)
            return
        if not interlock.tripped:
            self._set_led(interlock, 'running')
            interlock.status_label.setStyleSheet('color: #2ecc71; font-size: 10px;')
            interlock.status_label.setText('Armed')
            interlock.reset_button.setEnabled(False)
            return

        tripped_at = time.strftime('%H:%M:%S', time.localtime(interlock.trip_timestamp))
        if interlock.command_confirmed:
            self._set_led(interlock, 'crashed')
            interlock.status_label.setStyleSheet(f'color: {ALARM_COLOR}; font-size: 10px;')
            interlock.status_label.setText(
                f'TRIPPED {tripped_at} — {interlock.trip_reason}. {control.label} at '
                f'{interlock.safe_value:g} {control.units}; setpoint locked.')
        elif interlock.gave_up:
            self._set_led(interlock, 'crashed')
            interlock.status_label.setStyleSheet(f'color: {ALARM_COLOR}; font-weight: bold; font-size: 10px;')
            interlock.status_label.setText(
                f'TRIPPED {tripped_at} — {interlock.trip_reason}. COULD NOT SET {control.label} '
                f'to {interlock.safe_value:g} {control.units} — SHUT THE GAS MANUALLY.')
        else:
            self._set_led(interlock, 'sending')
            interlock.status_label.setStyleSheet(f'color: {ALARM_COLOR}; font-weight: bold; font-size: 10px;')
            interlock.status_label.setText(
                f'TRIPPED {tripped_at} — {interlock.trip_reason}. Setting {control.label} to '
                f'{interlock.safe_value:g} {control.units} (attempt {max(interlock.attempts, 1)} of '
                f'{INTERLOCK_MAX_ATTEMPTS})…')

        blocking = self._blocking_channels(interlock)
        interlock.reset_button.setEnabled(not blocking)
        if blocking:
            interlock.reset_button.setToolTip(
                'Reset blocked until the vessel pressure is readable again: '
                + ', '.join(f'{label} is {state}' for label, state in blocking))
        else:
            interlock.reset_button.setToolTip('Re-enables the setpoint control. The setpoint itself '
                                              'stays at the safe value until you set a new one.')

    # A bus dying takes every control on it down at once, and each has to say so --
    # a logger silently showing "running" against a closed port is the failure this
    # whole class exists to make impossible.
    def _on_bus_died(self, bus):
        exit_code = bus.process.returncode
        last_line = bus.stderr_lines[-1] if bus.stderr_lines else '(no stderr captured)'
        bus.user_stopped = True
        bus.ready = False
        bus.users.clear()
        self.plotter.log(f"[{bus.label}] bus exited unexpectedly (code {exit_code}): {last_line}",
                         level='ERROR')

        for logger in self.loggers.values():
            if logger.bus_id == bus.id and logger.running:
                logger.running = False
                logger.faulted = True
                logger.start_stop_button.setText(f'Start {logger.label}')
                logger.start_stop_button.setStyleSheet("background-color: green;")
                self._set_led(logger, 'crashed')
                logger.error_label.setText(f'shared port closed (exit {exit_code}): {last_line}')
                logger.error_label.show()

        for fill in list(self.fills.values()):
            if fill.engaged and self.setpoints[fill.setpoint_control_id].bus_id == bus.id:
                self._stop_fill(fill, 'ABORTED: shared port closed', stage='aborted')

        # An in-flight setpoint on a dead bus will never be answered; fail it now so
        # the control is usable again and a tripped interlock can retry.
        for control in self.setpoints.values():
            if control.bus_id == bus.id and control.sending:
                self._on_setpoint_result(control.id, 1, '', f'shared port closed: {last_line}')

        self._refresh_bus(bus)

    def cleanup(self):
        for bus in self.buses.values():
            self._stop_bus(bus)
        for logger in self.loggers.values():
            logger.user_stopped = True
            if logger.bus_id is None:
                self._kill(logger.process)
        # In-flight setpoint commands are deliberately NOT killed: they are bounded by
        # SETPOINT_TIMEOUT_S and killing one mid-write could leave the controller at a
        # value nobody asked for. Letting it finish is the safe end state.


class StatusStrip(QtWidgets.QWidget):
    '''Fixed-height row of tiles, always visible above the tabs. Declared once via
    LivePlotter.set_status_strip([...]) with plain channel ids and/or AggregateTile
    specs; refreshed every alarm-scan tick.'''

    def __init__(self, plotter):
        super().__init__()
        self.plotter = plotter
        self.tiles = []  # [(spec, value_label), ...]

        self.layout = QtWidgets.QHBoxLayout()
        self.setLayout(self.layout)

    def set_tiles(self, tile_specs):
        while self.layout.count():
            item = self.layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.tiles = []

        for spec in tile_specs:
            frame = QtWidgets.QFrame()
            frame.setFrameShape(QtWidgets.QFrame.Box)
            box = QtWidgets.QVBoxLayout()
            frame.setLayout(box)

            heading = spec.label if isinstance(spec, AggregateTile) else self.plotter.channels[spec].label
            box.addWidget(QtWidgets.QLabel(heading))
            value_label = QtWidgets.QLabel('—')
            # Fixed width, never wrapped: _set_value() elides to fit instead. A
            # word-wrapped label here re-derives its height from whatever width the
            # layout hands it, so every tick's text change could resize the strip.
            value_label.setFixedWidth(STATUS_TILE_VALUE_WIDTH)
            box.addWidget(value_label)

            frame.mousePressEvent = lambda event, s=spec: self._on_clicked(s)
            self.layout.addWidget(frame)
            self.tiles.append((spec, value_label))

        # Every tile is fixed-size now, so the strip's sizeHint is stable -- pin the
        # height so no later relayout (a tab switch re-activates the whole chain) can
        # settle on a taller strip and take that space from the tabs below.
        self.setFixedHeight(self.sizeHint().height())

    def _on_clicked(self, spec):
        if isinstance(spec, AggregateTile):
            self.plotter.jump_to_tab(spec.jump_to_tab)
        else:
            self.plotter.jump_to_channel(spec)

    def refresh(self, now):
        for spec, value_label in self.tiles:
            if isinstance(spec, AggregateTile):
                self._refresh_aggregate(spec, value_label, now)
            else:
                self._refresh_channel(spec, value_label, now)

    # Tiles are fixed-width, so long text is elided rather than allowed to resize the
    # strip; the untruncated reading stays available as a tooltip.
    def _set_value(self, value_label, text, style):
        apply_style(value_label, style)
        value_label.setText(value_label.fontMetrics().elidedText(text, QtCore.Qt.ElideRight, STATUS_TILE_VALUE_WIDTH))
        value_label.setToolTip(text)

    def _refresh_channel(self, channel_id, value_label, now):
        channel = self.plotter.channels[channel_id]
        state = self.plotter.alarm_evaluator.state_for(channel_id)
        status = display_status(state)
        text, style = format_channel_value(channel, state, status, offset=0.0, now=now)
        self._set_value(value_label, text, style)

    def _refresh_aggregate(self, spec, value_label, now):
        worst_channel_id = None
        worst_value = None
        any_alarm = False
        for channel_id in spec.channels:
            state = self.plotter.alarm_evaluator.state_for(channel_id)
            if display_status(state) == DisplayStatus.ALARM:
                any_alarm = True
            value = state.last_value
            if value is None or value != value:
                continue
            if worst_value is None or (spec.reduce == 'max' and value > worst_value) or (spec.reduce == 'min' and value < worst_value):
                worst_value = value
                worst_channel_id = channel_id

        if worst_channel_id is None:
            self._set_value(value_label, '—', 'color: grey;')
            return

        channel = self.plotter.channels[worst_channel_id]
        channel_index = channel.vmm_num if channel.vmm_num is not None else worst_channel_id
        self._set_value(
            value_label,
            f"{worst_value:.3g} {channel.units} (ch {channel_index:02d})",
            f"color: {ALARM_COLOR}; font-weight: bold;" if any_alarm else "",
        )


class AlarmBanner(QtWidgets.QWidget):
    '''Hidden when nothing is active. Shows the highest-priority active alarm plus a
    count when several are active. Latched: a cleared-but-unacknowledged alarm keeps
    the banner up (amber) until Acknowledge is clicked.'''

    PRIORITY = {DisplayStatus.ALARM: 0, DisplayStatus.STALE: 1, DisplayStatus.CLEARED: 2}

    def __init__(self, plotter):
        super().__init__()
        self.plotter = plotter
        self._current_channel_id = None

        self.layout = QtWidgets.QHBoxLayout()
        self.setLayout(self.layout)

        self.message_label = QtWidgets.QLabel('')
        self.message_label.setStyleSheet("font-weight: bold;")
        # A QLabel without word wrap has a minimumSizeHint equal to its full
        # (single-line) text width -- and Qt's layout-minimum-size constraint
        # overrides "maximized" window state. Since this label sits directly in
        # main_layout (not inside any scroll area) and its text length varies with
        # whatever alarm is currently active, an unwrapped long message could force
        # the whole window wider than any real screen the instant it appeared.
        #
        # Wrapping used to be the answer, but it trades a width problem for a height
        # one: the banner then grows a line at a time as the message lengthens, and
        # every line it gains comes out of the tabs/control dock below it. An Ignored
        # horizontal policy drops the width floor instead, and _apply_message()
        # elides to whatever width the banner actually has, so the message always
        # occupies exactly one line no matter how long it gets.
        self.message_label.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
        self._full_message = ''
        self.message_label.mousePressEvent = self._on_message_clicked
        self.layout.addWidget(self.message_label, 1)

        self.ack_button = QtWidgets.QPushButton('Acknowledge')
        self.ack_button.clicked.connect(self._on_acknowledge)
        self.layout.addWidget(self.ack_button)

        self.ack_all_button = QtWidgets.QPushButton('Acknowledge All')
        self.ack_all_button.clicked.connect(self._on_acknowledge_all)
        self.layout.addWidget(self.ack_all_button)

        # One row, always. The banner shares a fixed total height with main_splitter,
        # so any height of its own that follows its contents comes straight out of the
        # tabs/control dock below -- and its contents change every scan tick while an
        # alarm is up. Pin the height here (as StatusStrip does) so nothing it renders
        # can resize anything else; only appearing/disappearing moves the layout.
        self.setFixedHeight(self.sizeHint().height())

        # Ticks observed with nothing active, used to debounce hiding. A status that
        # flickers (a value dithering across its threshold, a channel going stale and
        # un-stale as samples trickle in) would otherwise show/hide the banner every
        # second, and each toggle shifts everything below it by the banner's height.
        self._empty_ticks = 0
        self.hide()

    def _active_alarms(self):
        results = []
        for channel_id in self.plotter.channels:
            state = self.plotter.alarm_evaluator.state_for(channel_id)
            status = display_status(state)
            if status in self.PRIORITY and not state.acknowledged:
                results.append((channel_id, status, state))
        results.sort(key=lambda item: self.PRIORITY[item[1]])
        return results

    HIDE_AFTER_EMPTY_TICKS = 3

    def refresh(self, now):
        active = self._active_alarms()
        if not active:
            self._empty_ticks += 1
            if self._empty_ticks >= self.HIDE_AFTER_EMPTY_TICKS:
                self._current_channel_id = None
                self.hide()
            return
        self._empty_ticks = 0

        channel_id, status, state = active[0]
        channel = self.plotter.channels[channel_id]
        message = self._format_message(channel, state, status, now)
        if len(active) > 1:
            message = f"⚠ {len(active)} alarms — {message}"
        else:
            message = f"⚠ {message}"

        self._full_message = message
        self._apply_message()
        self._current_channel_id = channel_id
        self.ack_all_button.setVisible(len(active) > 1)
        self.show()

    # Render the current message elided to the label's actual width, so the banner
    # stays exactly one line tall whatever the message says. Re-run on resize, since
    # the width to elide against is the window's.
    def _apply_message(self):
        width = self.message_label.width()
        if width <= 0:
            self.message_label.setText(self._full_message)
            return
        self.message_label.setText(
            self.message_label.fontMetrics().elidedText(self._full_message, QtCore.Qt.ElideRight, width))
        self.message_label.setToolTip(self._full_message)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply_message()

    def _format_message(self, channel, state, status, now):
        if status == DisplayStatus.ALARM:
            limit = limit_description(channel.alarm)
            return f"{channel.label} — {state.last_value:.3g} {channel.units} ({limit})"
        if status == DisplayStatus.STALE:
            age = (now - state.last_timestamp) if state.last_timestamp else 0
            return f"{channel.label} — stale, no data for {format_age(age)}"
        if status == DisplayStatus.CLEARED:
            trip_str = datetime.datetime.fromtimestamp(state.trip_timestamp).strftime('%H:%M:%S') if state.trip_timestamp else '?'
            return f"{channel.label} — cleared, peak {state.peak_value:.3g} {channel.units} at {trip_str}"
        return channel.label

    def _on_message_clicked(self, event):
        if self._current_channel_id is not None:
            self.plotter.jump_to_channel(self._current_channel_id)

    def _on_acknowledge(self):
        if self._current_channel_id is not None:
            for transition in self.plotter.alarm_evaluator.acknowledge(self._current_channel_id, now=time.time()):
                self.plotter.log(transition.message, level='INFO')
            self.refresh(time.time())

    def _on_acknowledge_all(self):
        for transition in self.plotter.alarm_evaluator.acknowledge_all(now=time.time()):
            self.plotter.log(transition.message, level='INFO')
        self.refresh(time.time())


class EventLog(QtWidgets.QWidget):
    '''Read-only, filterable log view, shown in its own "Event Terminal" tab (added
    by LivePlotter.run() so it lands after every tab launch_GUI.py creates). Every
    line is also mirrored to a file on disk (flushed on every write) so the log
    survives a GUI crash, same as the data logs it sits next to. Log files live in
    LOG_DIR, one per calendar date (not one per launch) so relaunching the program
    the same day keeps appending to that day's file; if the program is still running
    when the date changes, the next line written rolls over to a fresh file for the
    new day.

    Levels are free-form strings supplied by the caller (INFO, ERROR, ALARM, and NOTE
    for operator notes) rather than an enum, so the filter box searches them as plain
    text like everything else on the line.'''

    MAX_LINES = 5000
    LOG_DIR = 'event_logs'

    def __init__(self):
        super().__init__()
        self._lines = []  # formatted strings, capped at MAX_LINES, independent of the active filter
        self._file = None
        self._file_date = None

        layout = QtWidgets.QVBoxLayout()
        self.setLayout(layout)

        toolbar = QtWidgets.QHBoxLayout()
        self.filter_box = QtWidgets.QLineEdit()
        self.filter_box.setPlaceholderText('Filter…')
        self.filter_box.textChanged.connect(self._render)
        toolbar.addWidget(self.filter_box)
        copy_button = QtWidgets.QPushButton('Copy visible to clipboard')
        copy_button.clicked.connect(self._copy_visible)
        toolbar.addWidget(copy_button)
        layout.addLayout(toolbar)

        self.text_edit = QtWidgets.QPlainTextEdit()
        self.text_edit.setReadOnly(True)
        self.text_edit.setMaximumBlockCount(self.MAX_LINES)
        layout.addWidget(self.text_edit)

        self._open_log_file_for(datetime.date.today())

    def _log_path_for(self, date):
        os.makedirs(self.LOG_DIR, exist_ok=True)
        return os.path.join(self.LOG_DIR, f"event_log_{date.strftime('%Y-%m-%d')}.log")

    def _open_log_file_for(self, date):
        if self._file is not None:
            self._file.close()
        self._file = open(self._log_path_for(date), 'a', encoding='utf-8')
        self._file_date = date

    def add_line(self, level, message):
        today = datetime.date.today()
        if today != self._file_date:
            self._open_log_file_for(today)

        timestamp = datetime.datetime.now().strftime('%H:%M:%S')
        line = f"{timestamp}  {level:<5}  {message}"

        self._lines.append(line)
        if len(self._lines) > self.MAX_LINES:
            self._lines = self._lines[-self.MAX_LINES:]

        self._file.write(line + '\n')
        self._file.flush()

        if self._matches_filter(line):
            self.text_edit.appendPlainText(line)

    def _matches_filter(self, line):
        needle = self.filter_box.text().strip().lower()
        return not needle or needle in line.lower()

    def _render(self):
        needle = self.filter_box.text().strip().lower()
        visible = [line for line in self._lines if not needle or needle in line.lower()]
        self.text_edit.setPlainText('\n'.join(visible))

    def _copy_visible(self):
        QtWidgets.QApplication.clipboard().setText(self.text_edit.toPlainText())

    def close_file(self):
        try:
            self._file.close()
        except OSError:
            pass

    def alarming_channel_ids(self, evaluator):
        return set()  # the log isn't tied to specific channels, so it never gets its own badge


class OverviewTab(QtWidgets.QWidget):
    '''Dashboard: tiles grouped by each channel's overview_group, no live plots.
    Values/coloring are driven by the same alarm-scan tick as everything else (no
    extra read); sparklines run on their own slower timer since a 5-minute trend
    doesn't need 1s precision and re-reading every channel's history that often
    would only add to the read duplication commit 10 exists to fix.'''

    SPARKLINE_WINDOW_S = 300
    SPARKLINE_REFRESH_MS = 5000

    def __init__(self, plotter):
        super().__init__()
        self.plotter = plotter
        self.tiles = []  # [{'channel_id', 'frame', 'value_label', 'curve'}, ...]

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        container = QtWidgets.QWidget()
        self.layout = QtWidgets.QVBoxLayout(container)
        scroll.setWidget(container)
        outer_layout = QtWidgets.QVBoxLayout()
        outer_layout.addWidget(scroll)
        self.setLayout(outer_layout)

        self._build_groups()

        self._sparkline_timer = QtCore.QTimer()
        self._sparkline_timer.timeout.connect(self.refresh_sparklines)
        self._sparkline_timer.start(self.SPARKLINE_REFRESH_MS)
        self.refresh_sparklines()

    def _build_groups(self):
        groups = {}
        for channel in self.plotter.channels.values():
            if channel.overview_group is not None:
                groups.setdefault(channel.overview_group, []).append(channel)

        for group_name, channels in groups.items():
            box = QtWidgets.QGroupBox(group_name)
            grid = QtWidgets.QGridLayout()
            box.setLayout(grid)
            columns = 4
            for i, channel in enumerate(channels):
                tile = self._build_tile(channel)
                grid.addWidget(tile['frame'], i // columns, i % columns)
                self.tiles.append(tile)
            self.layout.addWidget(box)
        self.layout.addStretch(1)

    def _build_tile(self, channel):
        frame = QtWidgets.QFrame()
        frame.setFrameShape(QtWidgets.QFrame.Box)
        vbox = QtWidgets.QVBoxLayout()
        frame.setLayout(vbox)

        vbox.addWidget(QtWidgets.QLabel(channel.label))
        value_label = QtWidgets.QLabel('—')
        value_label.setWordWrap(True)  # cheap insurance against the same unwrapped-label minimum-width issue as the banner/status strip
        vbox.addWidget(value_label)

        sparkline = pg.PlotWidget()
        sparkline.setFixedSize(110, 34)
        sparkline.hideAxis('bottom')
        sparkline.hideAxis('left')
        sparkline.setMouseEnabled(x=False, y=False)
        sparkline.setMenuEnabled(False)
        curve = sparkline.plot(pen='c')
        vbox.addWidget(sparkline)

        frame.mousePressEvent = lambda event, cid=channel.id: self.plotter.jump_to_channel(cid)

        return {'channel_id': channel.id, 'frame': frame, 'value_label': value_label, 'curve': curve}

    def alarming_channel_ids(self, evaluator):
        return set()  # Overview never gets its own badge (§4.6.4 only shows one on VMM Temperatures in the mockup)

    def refresh_values(self, now):
        for tile in self.tiles:
            channel = self.plotter.channels[tile['channel_id']]
            state = self.plotter.alarm_evaluator.state_for(tile['channel_id'])
            status = display_status(state)
            text, style = format_channel_value(channel, state, status, offset=0.0, now=now)
            tile['value_label'].setText(text)
            apply_style(tile['value_label'], style)
            apply_style(tile['frame'], f"border: 2px solid {ALARM_COLOR};" if state.state == AlarmState.ALARM else "")

    def refresh_sparklines(self):
        for tile in self.tiles:
            channel = self.plotter.channels[tile['channel_id']]
            n = max(2, math.ceil(self.SPARKLINE_WINDOW_S / channel.log_interval_s))
            try:
                x_data, y_data = get_n_XY_datapoints(channel.filepath, n, channel.datatype, channel.vmm_num)
            # KeyError included for the same reason as in ScanRunnable.run(): a datatype
            # paired with a file that lacks its column raises it, and escaping this slot
            # would skip every remaining tile's sparkline for the tick.
            except (pd.errors.ParserError, ValueError, KeyError, FileNotFoundError, OSError):
                continue
            tile['curve'].setData(x=x_data, y=y_data)


class VMMTab(QtWidgets.QWidget):
    '''Replaces the wall of 16 individual plots with a 4x4 tile grid (checkbox +
    color swatch + current value + alarm color) beside one overlay plot of every
    checked channel. Curve data is pushed in centrally by
    LivePlotter._on_scan_finished(); this class only owns the widgets, checkbox
    state, and tile styling.

    Each tile carries its curve's color swatch, which makes the tile grid the
    overlay's legend -- a pyqtgraph legend with 32 entries is a wall of text that
    overflows the plot and makes the colors useless in practice, so the overlay has
    no legend of its own.'''

    def __init__(self, plotter, channel_ids, threshold=None):
        super().__init__()
        self.plotter = plotter
        self.channel_ids = list(channel_ids)
        self.tiles = {}   # channel_id -> {'frame', 'checkbox', 'value_label', 'swatch'}
        self.curves = {}  # channel_id -> PlotDataItem
        self.note_lines = []  # operator-note markers on the overlay, same pool as a LiveTab plot's
        # channel_id -> assigned pen color, so alarm highlighting can revert to it and
        # each tile's swatch can match its curve. Generated up front (before the tiles
        # are built) from the channel's *index*, so it's identical on every launch --
        # see core_tools/gui/palette.py.
        self._colors = {cid: color for cid, color in zip(self.channel_ids, channel_palette(len(self.channel_ids)))}
        self.paused = False
        # Same follow/frozen failure mode as a LiveTab plot (§1). The overlay has no
        # per-plot header row, so its indicator goes in the controls row below,
        # beside Select All / Select None. The button is built with that row, after
        # the overlay it reports on, so it starts as None and the refresh below
        # tolerates that.
        self.following = True
        self.follow_button = None

        # Overlay plot on top (gets the dominant share of the space -- it's the
        # thing operators actually need to see) with the tile grid + Select
        # All/None controls in a compact strip underneath, not squeezed beside it.
        splitter = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        splitter.setHandleWidth(SPLITTER_HANDLE_WIDTH)

        self.overlay_widget = pg.PlotWidget(title='VMM Temperatures Overlay')
        self.overlay_widget.setLabel('bottom', 'Time since present', units='s')
        self.overlay_widget.setLabel('left', 'Temperature', units='degC')
        self.overlay_widget.showGrid(x=True, y=True)
        self.overlay_widget.getViewBox().sigStateChanged.connect(
            lambda _vb: self._on_view_state_changed())
        # No addLegend() here on purpose: the tile grid below *is* the legend (each
        # tile shows its curve's color swatch). At 32 channels an in-plot legend
        # overflows the overlay and buries the data it's meant to explain.
        splitter.addWidget(self.overlay_widget)

        bottom_widget = QtWidgets.QWidget()
        bottom_layout = QtWidgets.QVBoxLayout()
        bottom_layout.setContentsMargins(4, 4, 4, 4)
        bottom_widget.setLayout(bottom_layout)

        controls_row = QtWidgets.QHBoxLayout()
        select_all_button = QtWidgets.QPushButton('Select All')
        select_all_button.clicked.connect(self.select_all)
        controls_row.addWidget(select_all_button)
        select_none_button = QtWidgets.QPushButton('Select None')
        select_none_button.clicked.connect(self.select_none)
        controls_row.addWidget(select_none_button)
        self.follow_button = make_follow_indicator(self.resume_live)
        self.follow_button.setFixedWidth(FOLLOW_INDICATOR_WIDTH)
        controls_row.addWidget(self.follow_button)
        controls_row.addStretch(1)
        bottom_layout.addLayout(controls_row)

        grid = QtWidgets.QGridLayout()
        grid.setSpacing(2)
        columns = 4
        for i, channel_id in enumerate(self.channel_ids):
            tile = self._build_tile(channel_id)
            grid.addWidget(tile['frame'], i // columns, i % columns)
            self.tiles[channel_id] = tile
        bottom_layout.addLayout(grid)

        # Wrapped in a scroll area (rather than added to the splitter directly) so
        # the tile grid can grow past 16 VMMs later -- extra rows scroll instead of
        # squeezing the overlay plot or widening the window.
        bottom_scroll = QtWidgets.QScrollArea()
        bottom_scroll.setWidgetResizable(True)
        bottom_scroll.setWidget(bottom_widget)
        # setWidgetResizable(True) folds the content widget's minimum size into the
        # scroll area's own, so without an explicit minimum the grid still demands
        # room from the splitter (and the window) instead of scrolling -- which is
        # the entire point of the scroll area. Cap it and let it scroll.
        bottom_scroll.setMinimumSize(200, 80)

        splitter.addWidget(bottom_scroll)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        # setStretchFactor only governs resize behavior, not the initial split (which
        # QSplitter otherwise derives from sizeHint(), letting 16 tiles' natural width
        # dominate and squeeze the plot down to a sliver) -- pin a sane initial split.
        splitter.setSizes([600, 200])

        if threshold is None:
            for channel_id in self.channel_ids:
                alarm = self.plotter.channels[channel_id].alarm
                if alarm is not None and alarm.high is not None:
                    threshold = alarm.high
                    break
        if threshold is not None:
            line = pg.InfiniteLine(pos=threshold, angle=0, pen=pg.mkPen(ALARM_COLOR, style=QtCore.Qt.DashLine))
            self.overlay_widget.addItem(line)

        for channel_id in self.channel_ids:
            self.curves[channel_id] = self.overlay_widget.plot(pen=pg.mkPen(self._colors[channel_id], width=1))
            style_point_markers(self.curves[channel_id], self._colors[channel_id])

        outer_layout = QtWidgets.QVBoxLayout()
        outer_layout.addWidget(splitter)
        self.setLayout(outer_layout)

    def _build_tile(self, channel_id):
        channel = self.plotter.channels[channel_id]
        fec, remainder = divmod(channel.vmm_num, 8)
        hyb, vmm = divmod(remainder, 2)

        frame = QtWidgets.QFrame()
        frame.setFrameShape(QtWidgets.QFrame.Box)
        row = QtWidgets.QHBoxLayout()
        row.setContentsMargins(4, 1, 4, 1)
        row.setSpacing(4)
        frame.setLayout(row)

        checkbox = QtWidgets.QCheckBox()
        checkbox.setChecked(True)
        checkbox.setStyleSheet("QCheckBox::indicator { width: 18px; height: 18px; }")
        checkbox.stateChanged.connect(lambda state, cid=channel_id: self._on_checkbox_changed(cid, state))
        row.addWidget(checkbox)

        # This swatch is what makes the tile grid the overlay's legend. Fixed-size and
        # never restyled after construction, so it can't feed a scan tick's text
        # changes into the grid's geometry.
        swatch = QtWidgets.QLabel()
        swatch.setFixedSize(SWATCH_SIZE, SWATCH_SIZE)
        swatch.setStyleSheet(f"background-color: {self._colors[channel_id]}; border: 1px solid #333;")
        swatch.setToolTip(f"Overlay curve color for {channel.label}")
        row.addWidget(swatch)

        label = QtWidgets.QLabel(f"VMM {channel.vmm_num} (F{fec}/H{hyb}/V{vmm})")
        label.setStyleSheet("font-size: 10px;")
        row.addWidget(label)

        value_label = QtWidgets.QLabel('—')
        value_label.setStyleSheet("font-size: 10px;")
        value_label.setWordWrap(True)  # keeps a stale/long reading from widening the tile grid inside the scroll area
        row.addWidget(value_label)
        row.addStretch(1)

        return {'frame': frame, 'checkbox': checkbox, 'value_label': value_label, 'swatch': swatch}

    def _on_checkbox_changed(self, channel_id, state):
        self.curves[channel_id].setVisible(state == QtCore.Qt.Checked)

    def select_all(self):
        for tile in self.tiles.values():
            tile['checkbox'].setChecked(True)

    def select_none(self):
        for tile in self.tiles.values():
            tile['checkbox'].setChecked(False)

    def pause(self):
        self.paused = True
        self._refresh_follow_indicator()  # a paused overlay isn't showing live data either

    def resume(self):
        self.paused = False
        self._refresh_follow_indicator()

    def _refresh_follow_indicator(self):
        if self.follow_button is not None:
            set_follow_indicator(self.follow_button, following=self.following, paused=self.paused)

    # Same derivation as LiveTab._on_view_state_changed -- see is_following() for why
    # this reads the ViewBox instead of remembering one signal.
    def _on_view_state_changed(self):
        following = is_following(self.overlay_widget)
        if following != self.following:
            self.following = following
            self._refresh_follow_indicator()

    def resume_following(self):
        resume_following_on(self.overlay_widget)
        self.following = True
        self._refresh_follow_indicator()

    # The indicator's own click, as on a LiveTab plot: unpause and re-follow.
    def resume_live(self):
        self.paused = False
        self.resume_following()

    def alarming_channel_ids(self, evaluator):
        return {cid for cid in self.channel_ids if display_status(evaluator.state_for(cid)) in (DisplayStatus.ALARM, DisplayStatus.STALE)}

    # Tile value/color, auto-check + curve highlight on ALARM entry. Called every
    # alarm-scan tick, same as the status strip/overview/per-plot readouts.
    def refresh(self, now):
        for channel_id, tile in self.tiles.items():
            channel = self.plotter.channels[channel_id]
            state = self.plotter.alarm_evaluator.state_for(channel_id)
            status = display_status(state)

            text, style = format_channel_value(channel, state, status, offset=0.0, now=now)
            tile['value_label'].setText(text)
            apply_style(tile['value_label'], style)
            apply_style(tile['frame'], f"border: 2px solid {ALARM_COLOR};" if state.state == AlarmState.ALARM else "")

            curve = self.curves[channel_id]
            if state.state == AlarmState.ALARM:
                curve.setPen(pg.mkPen(ALARM_COLOR, width=3))
                if not tile['checkbox'].isChecked():
                    tile['checkbox'].setChecked(True)  # emits stateChanged -> curve becomes visible
            else:
                curve.setPen(pg.mkPen(self._colors[channel_id], width=1))
