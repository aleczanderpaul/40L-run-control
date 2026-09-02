from pyqtgraph.Qt import QtWidgets, QtCore
import pyqtgraph as pg
import sys
import os
import time
import math
import datetime
import pandas as pd
import subprocess
import threading
import platform
import serial.tools.list_ports

from .get_data_for_GUI import get_n_XY_datapoints
from .models import Channel, Plot, LoggerControl, AggregateTile
from .decimate import decimate_min_max
from .data_cache import ScanRunnable
from core_tools.alarms import AlarmEvaluator, AlarmState, DisplayStatus, display_status

'''Class to handle live plotting and add various controls/buttons in a Qt GUI application.'''

COLOR_CYCLE = ['y', 'c', 'm', 'r', 'g', 'b', 'w']

LED_COLORS = {'running': '#2ecc71', 'stopped': '#95a5a6', 'crashed': '#e74c3c'}
ALARM_COLOR = '#e74c3c'

# How often the alarm scanner reads every registered channel and how many trailing
# rows it fetches per read. Deliberately independent of any plot's own timer (a
# paused plot must not pause its channels' alarm evaluation) and, for now,
# independent of the per-plot update() reads too -- both re-read from disk until the
# shared data cache lands, which is a known, temporary duplication of file reads.
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


def rows_for_window(window_s, log_interval_s):
    return max(2, math.ceil(window_s / log_interval_s))


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
        return f"{text} (stale {age:.0f}s)", "color: grey;"
    if status == DisplayStatus.CLEARED:
        return text, "color: #b8860b;"
    return text, ""


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
        self.scan_timer = QtCore.QTimer()
        self.scan_timer.timeout.connect(self._start_scan)
        self.scan_timer.start(ALARM_SCAN_INTERVAL_MS)

        # Calls the cleanup function when the application is about to quit so that all running subprocesses are terminated
        self.app.aboutToQuit.connect(self.cleanup)

    def log(self, message, level='INFO'):
        self.event_log.add_line(level, message)

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
            n = max(ALARM_LOOKBACK_ROWS, rows_for_window(self.window_seconds, channel.log_interval_s))
            requests.append((channel_id, channel.filepath, n, channel.datatype, channel.vmm_num))

        runnable = ScanRunnable(requests)
        runnable.signals.finished.connect(self._on_scan_finished)
        QtCore.QThreadPool.globalInstance().start(runnable)

    # Runs on the GUI thread once the background read completes: feeds newly-arrived
    # samples (not just the newest) through the alarm evaluator, and redraws every
    # unpaused plot/VMM-overlay curve referencing each channel.
    def _on_scan_finished(self, results):
        self._scan_in_flight = False
        now = time.time()

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
            if newest_ts is not None:
                self._alarm_last_ts[channel_id] = newest_ts

            # Decimate once per channel (independent of which plot/offset uses it)
            # and reuse for every consumer of this channel's curve.
            dec_x, dec_y = decimate_min_max(x_data, y_data, max_points=DECIMATION_CAP)

            for tab, plot_id in self.channel_plots.get(channel_id, []):
                plot = tab.plots.get(plot_id)
                if plot is None or not plot.running:
                    continue
                idx = plot.channel_ids.index(channel_id)
                plot.curves[idx].setData(x=dec_x, y=dec_y + float(plot.offsets[idx]))

            for tab in self.tab_objects.values():
                if isinstance(tab, VMMTab) and not tab.paused and channel_id in tab.curves:
                    tab.curves[channel_id].setData(x=dec_x, y=dec_y)

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
            self.tabs.setTabText(index, f"{tab_name} ●{len(alarming)}" if alarming else tab_name)

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
        label.setStyleSheet(style)

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

        # Color mapping lives in a legend, not in the plot title, whenever a plot
        # overlays more than one channel.
        multi_curve = len(plot.channel_ids) > 1
        if multi_curve:
            plot_widget.addLegend()

        for i, channel_id in enumerate(plot.channel_ids):
            channel = self._channel(channel_id)
            color = COLOR_CYCLE[i % len(COLOR_CYCLE)]
            curve = plot_widget.plot(pen=color, name=channel.label if multi_curve else None)
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

    # Register a logger's controls (LED, port, interval, start/stop) in the shared
    # control dock -- see ControlDock.add_logger_group for what this actually builds.
    def add_logger_control(self, id, label, script, log_filepath, port, interval_options, default_interval):
        return self.plotter.control_dock.add_logger_group(
            id=id, label=label, script=script, log_filepath=log_filepath, port=port,
            interval_options=interval_options, default_interval=default_interval,
        )

class ControlDock(QtWidgets.QWidget):
    '''Persistent right-hand dock, visible regardless of which tab is selected. Owns
    every logger's structured subprocess lifecycle (LED, port/interval dropdowns,
    start/stop, crash reporting) -- LiveTab.add_logger_control() just forwards here.'''

    # stderr is read on a background thread (see _read_stderr); a signal is the only
    # safe way to hand a line back to the GUI thread for logging/widget updates --
    # Qt widgets must never be touched directly from a non-GUI thread.
    stderr_line_received = QtCore.pyqtSignal(str, str)  # logger_id, line

    def __init__(self, plotter):
        super().__init__()
        self.plotter = plotter
        self.loggers = {}  # id -> LoggerControl

        self.layout = QtWidgets.QVBoxLayout()
        self.setLayout(self.layout)

        self.loggers_layout = QtWidgets.QVBoxLayout()
        self.layout.addLayout(self.loggers_layout)

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

        self.layout.addStretch(1)

        self.stderr_line_received.connect(self._on_stderr_line)

        # Polls every logger's subprocess for an unexpected exit; this is deliberately
        # decoupled from any single logger's own start/stop so a crash is caught even
        # if nothing else touches that logger's controls.
        self._status_timer = QtCore.QTimer()
        self._status_timer.timeout.connect(self._poll_loggers)
        self._status_timer.start(500)

    def _set_led(self, logger, state):
        logger.led.setStyleSheet(f"background-color: {LED_COLORS[state]}; border-radius: 6px;")

    def _on_window_changed(self, idx):
        seconds = self.window_combo.itemData(idx)
        self.plotter.window_seconds = seconds
        self.plotter.settings.setValue('window_seconds', seconds)

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

    # Build one logger's group box: LED, port dropdown (from the live serial port
    # list, defaulting to the declared port), interval dropdown, start/stop button,
    # and a hidden error line that appears only on an unexpected exit.
    def add_logger_group(self, id, label, script, log_filepath, port, interval_options, default_interval):
        logger = LoggerControl(
            id=id, label=label, script=script, log_filepath=log_filepath,
            interval_options=interval_options, default_interval=default_interval, port=port,
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

        port_combo = QtWidgets.QComboBox()
        available_ports = [p.device for p in serial.tools.list_ports.comports()]
        if port not in available_ports:
            available_ports = [port] + available_ports
        port_combo.addItems(available_ports)
        port_combo.setCurrentText(port)
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

        self.loggers[id] = logger
        self._set_led(logger, 'stopped')
        self.loggers_layout.addWidget(box)
        return logger

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
        port = logger.port_combo.currentText()
        # argv is a real list -- never a shell string -- so filenames/ports with
        # spaces need no special handling, and sys.executable ensures the venv
        # interpreter (not a bare 'python' off PATH) runs the logger.
        argv = [sys.executable, logger.script, logger.log_filepath, port, str(interval)]

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
        logger.start_stop_button.setText(f'Stop {logger.label}')
        logger.start_stop_button.setStyleSheet("background-color: red;")
        logger.error_label.hide()
        self._set_led(logger, 'running')
        self.plotter.log(f"Started logger: {logger.label} ({port}, {interval}s)", level='INFO')

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
        self._kill(logger.process)
        logger.running = False
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
        for logger in self.loggers.values():
            if logger.running and logger.process is not None and logger.process.poll() is not None:
                exit_code = logger.process.returncode
                logger.running = False
                logger.start_stop_button.setText(f'Start {logger.label}')
                logger.start_stop_button.setStyleSheet("background-color: green;")
                if logger.user_stopped:
                    self._set_led(logger, 'stopped')
                else:
                    self._set_led(logger, 'crashed')
                    last_line = logger.stderr_lines[-1] if logger.stderr_lines else '(no stderr captured)'
                    logger.error_label.setText(f'exit code {exit_code}: {last_line}')
                    logger.error_label.show()
                    self.plotter.log(f"Logger {logger.label} exited unexpectedly (code {exit_code}): {last_line}", level='ERROR')

    def cleanup(self):
        for logger in self.loggers.values():
            logger.user_stopped = True
            self._kill(logger.process)


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
            # This tile sits directly in main_layout (no scroll area above it), so an
            # unwrapped label's full-text-width minimumSizeHint would otherwise
            # become a hard floor under the whole window -- see AlarmBanner.
            value_label.setWordWrap(True)
            box.addWidget(value_label)

            frame.mousePressEvent = lambda event, s=spec: self._on_clicked(s)
            self.layout.addWidget(frame)
            self.tiles.append((spec, value_label))

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

    def _refresh_channel(self, channel_id, value_label, now):
        channel = self.plotter.channels[channel_id]
        state = self.plotter.alarm_evaluator.state_for(channel_id)
        status = display_status(state)
        text, style = format_channel_value(channel, state, status, offset=0.0, now=now)
        value_label.setText(text)
        value_label.setStyleSheet(style)

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
            value_label.setText('—')
            value_label.setStyleSheet('color: grey;')
            return

        channel = self.plotter.channels[worst_channel_id]
        channel_index = channel.vmm_num if channel.vmm_num is not None else worst_channel_id
        value_label.setText(f"{worst_value:.3g} {channel.units} (ch {channel_index})")
        value_label.setStyleSheet(f"color: {ALARM_COLOR}; font-weight: bold;" if any_alarm else "")


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
        self.message_label.setWordWrap(True)
        self.message_label.mousePressEvent = self._on_message_clicked
        self.layout.addWidget(self.message_label, 1)

        self.ack_button = QtWidgets.QPushButton('Acknowledge')
        self.ack_button.clicked.connect(self._on_acknowledge)
        self.layout.addWidget(self.ack_button)

        self.ack_all_button = QtWidgets.QPushButton('Acknowledge All')
        self.ack_all_button.clicked.connect(self._on_acknowledge_all)
        self.layout.addWidget(self.ack_all_button)

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

    def refresh(self, now):
        active = self._active_alarms()
        if not active:
            self._current_channel_id = None
            self.hide()
            return

        channel_id, status, state = active[0]
        channel = self.plotter.channels[channel_id]
        message = self._format_message(channel, state, status, now)
        if len(active) > 1:
            message = f"⚠ {len(active)} alarms — {message}"
        else:
            message = f"⚠ {message}"

        self.message_label.setText(message)
        self._current_channel_id = channel_id
        self.ack_all_button.setVisible(len(active) > 1)
        self.show()

    def _format_message(self, channel, state, status, now):
        if status == DisplayStatus.ALARM:
            limit = limit_description(channel.alarm)
            return f"{channel.label} — {state.last_value:.3g} {channel.units} ({limit})"
        if status == DisplayStatus.STALE:
            age = (now - state.last_timestamp) if state.last_timestamp else 0
            return f"{channel.label} — stale, no data for {age:.0f}s"
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
    new day.'''

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
            tile['value_label'].setStyleSheet(style)
            tile['frame'].setStyleSheet(f"border: 2px solid {ALARM_COLOR};" if state.state == AlarmState.ALARM else "")

    def refresh_sparklines(self):
        for tile in self.tiles:
            channel = self.plotter.channels[tile['channel_id']]
            n = max(2, math.ceil(self.SPARKLINE_WINDOW_S / channel.log_interval_s))
            try:
                x_data, y_data = get_n_XY_datapoints(channel.filepath, n, channel.datatype, channel.vmm_num)
            except (pd.errors.ParserError, ValueError, FileNotFoundError, OSError):
                continue
            tile['curve'].setData(x=x_data, y=y_data)


class VMMTab(QtWidgets.QWidget):
    '''Replaces the wall of 16 individual plots with a 4x4 tile grid (checkbox +
    current value + alarm color) beside one overlay plot of every checked channel.
    Curve data is pushed in centrally by LivePlotter._on_scan_finished(); this class
    only owns the widgets, checkbox state, and tile styling.'''

    def __init__(self, plotter, channel_ids, threshold=None):
        super().__init__()
        self.plotter = plotter
        self.channel_ids = list(channel_ids)
        self.tiles = {}   # channel_id -> {'frame', 'checkbox', 'value_label'}
        self.curves = {}  # channel_id -> PlotDataItem
        self._colors = {}  # channel_id -> assigned pen color, so alarm highlighting can revert to it
        self.paused = False

        # Overlay plot on top (gets the dominant share of the space -- it's the
        # thing operators actually need to see) with the tile grid + Select
        # All/None controls in a compact strip underneath, not squeezed beside it.
        splitter = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        splitter.setHandleWidth(SPLITTER_HANDLE_WIDTH)

        self.overlay_widget = pg.PlotWidget(title='VMM Temperatures Overlay')
        self.overlay_widget.setLabel('bottom', 'Time since present', units='s')
        self.overlay_widget.setLabel('left', 'Temperature', units='degC')
        self.overlay_widget.showGrid(x=True, y=True)
        self.overlay_widget.addLegend()
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

        splitter.addWidget(bottom_widget)
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

        for i, channel_id in enumerate(self.channel_ids):
            channel = self.plotter.channels[channel_id]
            color = COLOR_CYCLE[i % len(COLOR_CYCLE)]
            self._colors[channel_id] = color
            self.curves[channel_id] = self.overlay_widget.plot(pen=color, name=channel.label)

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
        checkbox.stateChanged.connect(lambda state, cid=channel_id: self._on_checkbox_changed(cid, state))
        row.addWidget(checkbox)

        label = QtWidgets.QLabel(f"VMM {channel.vmm_num} (F{fec}/H{hyb}/V{vmm})")
        label.setStyleSheet("font-size: 10px;")
        row.addWidget(label)

        value_label = QtWidgets.QLabel('—')
        value_label.setStyleSheet("font-size: 10px;")
        value_label.setWordWrap(True)  # VMMTab has no scroll area, so an unwrapped label here would push the window wider -- see AlarmBanner
        row.addWidget(value_label)
        row.addStretch(1)

        return {'frame': frame, 'checkbox': checkbox, 'value_label': value_label}

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

    def resume(self):
        self.paused = False

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
            tile['value_label'].setStyleSheet(style)
            tile['frame'].setStyleSheet(f"border: 2px solid {ALARM_COLOR};" if state.state == AlarmState.ALARM else "")

            curve = self.curves[channel_id]
            if state.state == AlarmState.ALARM:
                curve.setPen(pg.mkPen(ALARM_COLOR, width=3))
                if not tile['checkbox'].isChecked():
                    tile['checkbox'].setChecked(True)  # emits stateChanged -> curve becomes visible
            else:
                curve.setPen(pg.mkPen(self._colors[channel_id], width=1))
