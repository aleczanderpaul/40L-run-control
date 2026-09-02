from pyqtgraph.Qt import QtWidgets, QtCore
import pyqtgraph as pg
import sys
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

        # Alarm banner (hidden when nothing is active) and the status strip are
        # always visible above the tabs, regardless of which tab is selected.
        self.alarm_banner = AlarmBanner(self)
        self.status_strip = StatusStrip(self)

        # Tabs (left) and the persistent control dock (right) sit side by side and
        # also stay visible regardless of which tab is selected.
        self.tabs = QtWidgets.QTabWidget()
        self.control_dock = ControlDock(self)

        self.main_splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self.main_splitter.addWidget(self.tabs)
        self.main_splitter.addWidget(self.control_dock)
        self.main_splitter.setStretchFactor(0, 4)
        self.main_splitter.setStretchFactor(1, 1)

        top_widget = QtWidgets.QWidget()
        top_layout = QtWidgets.QVBoxLayout()
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_widget.setLayout(top_layout)
        top_layout.addWidget(self.alarm_banner)
        top_layout.addWidget(self.status_strip)
        top_layout.addWidget(self.main_splitter)

        # The control dock and the event log pane are the two collapsible regions
        # (drag the splitter handle to 0 to collapse); the banner/strip/tabs above
        # them are always visible instead, same as the doc's mockup.
        self.outer_splitter = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        self.outer_splitter.addWidget(top_widget)
        self.outer_splitter.addWidget(self.event_log)
        self.outer_splitter.setStretchFactor(0, 4)
        self.outer_splitter.setStretchFactor(1, 1)
        self.main_layout.addWidget(self.outer_splitter)

        self._restore_layout()

        # Alarm evaluation is driven by one scanner timer here, independent of any
        # plot's own timer/pause state (see core_tools/alarms.py and §4.4).
        self.alarm_evaluator = AlarmEvaluator()
        self._alarm_last_ts = {}  # channel_id -> newest absolute timestamp already evaluated
        self.alarm_scan_timer = QtCore.QTimer()
        self.alarm_scan_timer.timeout.connect(self._scan_alarms)
        self.alarm_scan_timer.start(ALARM_SCAN_INTERVAL_MS)

        # Calls the cleanup function when the application is about to quit so that all running subprocesses are terminated
        self.app.aboutToQuit.connect(self.cleanup)

    def log(self, message, level='INFO'):
        self.event_log.add_line(level, message)

    def _restore_layout(self):
        outer_sizes = self.settings.value('outer_splitter_sizes')
        if outer_sizes:
            self.outer_splitter.setSizes([int(s) for s in outer_sizes])
        main_sizes = self.settings.value('main_splitter_sizes')
        if main_sizes:
            self.main_splitter.setSizes([int(s) for s in main_sizes])

    def _save_layout(self):
        self.settings.setValue('outer_splitter_sizes', self.outer_splitter.sizes())
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

    # Read every registered channel's recent rows, feed the newly-arrived ones (not
    # just the newest) through the alarm evaluator, and refresh every plot's value
    # readout/border from the result. Runs regardless of which plots are paused or
    # which tab is selected.
    def _scan_alarms(self):
        now = time.time()
        for channel_id, channel in self.channels.items():
            try:
                times_ago, values = get_n_XY_datapoints(channel.filepath, ALARM_LOOKBACK_ROWS, channel.datatype, channel.vmm_num)
            except (pd.errors.ParserError, ValueError, FileNotFoundError, OSError):
                continue  # can't read right now (e.g. an external logger hasn't written yet) -- retry next tick

            last_seen = self._alarm_last_ts.get(channel_id)
            newest_ts = last_seen
            samples = []
            for seconds_ago, value in zip(times_ago, values):
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
            for plot in tab.plots.values():
                any_alarm = any(self.alarm_evaluator.state_for(cid).state == AlarmState.ALARM for cid in plot.channel_ids)
                self._set_plot_alarm_border(plot, any_alarm)

        self.status_strip.refresh(now)
        self.alarm_banner.refresh(now)
        self._refresh_tab_badges()
        if self.overview_tab is not None:
            self.overview_tab.refresh_values(now)

    def _refresh_tab_badges(self):
        for tab_name, tab in self.tab_objects.items():
            alarming = set()
            for plot in tab.plots.values():
                for channel_id in plot.channel_ids:
                    status = display_status(self.alarm_evaluator.state_for(channel_id))
                    if status in (DisplayStatus.ALARM, DisplayStatus.STALE):
                        alarming.add(channel_id)
            index = self.tabs.indexOf(tab)
            if index < 0:
                continue
            self.tabs.setTabText(index, f"{tab_name} ●{len(alarming)}" if alarming else tab_name)

    # Select the tab containing a channel's plot and scroll that plot into view --
    # used by status-strip/overview tile clicks and the alarm banner's message.
    def jump_to_channel(self, channel_id):
        refs = self.channel_plots.get(channel_id, [])
        if not refs:
            return
        tab, plot_id = refs[0]
        self.tabs.setCurrentWidget(tab)
        tab.scroll_to_plot(plot_id)

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

    # End all running logger subprocesses
    def cleanup(self):
        self._save_layout()
        self.control_dock.cleanup()
        self.event_log.close_file()

    # Show the window and start the event loop
    def run(self):
        self.main_window.show()
        sys.exit(self.app.exec_())


class LiveTab(QtWidgets.QWidget):
    def __init__(self, plots_per_row, plotter):
        super().__init__()  # Call the constructor of the parent class (QWidget) to properly initialize the widget. This class is now a custom QTWidget

        self.plotter = plotter  # back-reference, needed to resolve channel ids
        self.tab_name = None    # set by LivePlotter.create_tab() right after construction

        # Create a scroll area
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        self.scroll_area = scroll  # kept for scroll_to_plot()

        # Container widget inside the scroll area
        container = QtWidgets.QWidget()
        self.layout = QtWidgets.QGridLayout(container)

        scroll.setWidget(container)

        # Main layout for the tab is just the scroll area
        outer_layout = QtWidgets.QVBoxLayout()
        outer_layout.addWidget(scroll)
        self.setLayout(outer_layout)

        self.plots_per_row = plots_per_row
        self.plot_counts = 0

        self.plots = {}  # plot_id -> Plot (identity/config/runtime all in one place)

        # Internal state tracking for dropdown menus
        self.dd_menus = {}
        self.dd_option_names = {}
        self.dd_option_values = {}

    def _channel(self, channel_id):
        return self.plotter.channels[channel_id]

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
    def add_plot(self, plot_id, title, channels, x_axis, y_axis, offsets, buffer_size=10, group=None):
        # x_axis and y_axis are tuples of (label, unit); buffer_size is the number of
        # data points to display at once (temporary -- superseded by the time-window
        # selector); channels is a list of channel ids registered via add_channel.
        index = self.plot_counts
        plots_per_row = self.plots_per_row
        self.plot_counts += 1
        row = index // plots_per_row
        col = index % plots_per_row

        plot = Plot(
            plot_id=plot_id, title=title, channel_ids=list(channels), x_axis=x_axis, y_axis=y_axis,
            offsets=list(offsets), group=group, buffer_size=buffer_size,
        )

        container = QtWidgets.QVBoxLayout()

        header_row = QtWidgets.QHBoxLayout()
        for channel_id in plot.channel_ids:
            value_label = QtWidgets.QLabel(f"{self._channel(channel_id).label}: —")
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

        container.addWidget(plot_widget)

        # Wrap the layout in a QWidget and add it to the grid
        container_widget = QtWidgets.QWidget()
        container_widget.setLayout(container)
        container_widget.setMinimumSize(25 * 16, 40 * 9)
        self.layout.addWidget(container_widget, row, col)
        plot.container_widget = container_widget

        return plot

    # Scroll a specific plot into view within this tab's scroll area -- used when a
    # status-strip tile or the alarm banner's message is clicked.
    def scroll_to_plot(self, plot_id):
        plot = self.plots.get(plot_id)
        if plot is not None and plot.container_widget is not None:
            self.scroll_area.ensureWidgetVisible(plot.container_widget)

    # Update function: fetches data from file and updates the plot
    def update(self, plot_id):
        plot = self.plots[plot_id]
        for i, channel_id in enumerate(plot.channel_ids):
            channel = self._channel(channel_id)
            offset = plot.offsets[i]

            try:
                x_data, y_data = get_n_XY_datapoints(channel.filepath, plot.buffer_size, channel.datatype, channel.vmm_num)
            except (pd.errors.ParserError, ValueError) as e:
                self.plotter.log(f"[{plot.title}] update failed: {e}", level='ERROR')
                continue

            plot.curves[i].setData(x=x_data, y=y_data + float(offset))

    # Return elapsed time in seconds since the plot started
    def get_elapsed_time(self, plot_id):
        return self.plots[plot_id].elapsed_timer.elapsed() / 1000.0  # convert ms to seconds

    # Starts the QTimer that drives the updates for a given plot
    def start_timer(self, plot_id, interval_ms):
        plot = self.plots[plot_id]

        # Create a timer to update the plot regularly
        timer = QtCore.QTimer()
        timer.timeout.connect(lambda: self.update(plot_id))
        timer.start(interval_ms)
        plot.interval_timer = timer

        # Start a timer to track elapsed time
        elapsed = QtCore.QElapsedTimer()
        elapsed.start()
        plot.elapsed_timer = elapsed

        # Mark the plot as running
        plot.running = True

    # Toggle between running and paused for a given plot. This only stops/starts the
    # curve redraw -- the channel's alarm evaluation is driven entirely by
    # LivePlotter's independent scanner timer and keeps running regardless (§4.4).
    def toggle_plot(self, plot_id):
        plot = self.plots[plot_id]
        if plot.running:
            plot.interval_timer.stop()
            plot.pause_button.setText("▶")
            plot.pause_button.setToolTip(f"Resume {plot.title}")
            plot.running = False
        else:
            plot.elapsed_timer.restart()
            plot.interval_timer.start()
            plot.pause_button.setText("⏸")
            plot.pause_button.setToolTip(f"Pause {plot.title}")
            plot.running = True

    # Register a logger's controls (LED, port, interval, start/stop) in the shared
    # control dock -- see ControlDock.add_logger_group for what this actually builds.
    def add_logger_control(self, id, label, script, log_filepath, port, interval_options, default_interval):
        return self.plotter.control_dock.add_logger_group(
            id=id, label=label, script=script, log_filepath=log_filepath, port=port,
            interval_options=interval_options, default_interval=default_interval,
        )

    # Add a dropdown menu with specified options and values attached to the options
    def add_dropdown_menu(self, title, option_names, option_values, ctrl_var=None, on_change_callback=None):
        index = self.plot_counts
        plots_per_row = self.plots_per_row
        self.plot_counts += 1
        row = index // plots_per_row
        col = index % plots_per_row

        # Vertical layout to hold the plot and button
        container = QtWidgets.QVBoxLayout()

        # Label
        label = QtWidgets.QLabel(title)
        container.addWidget(label)

        # Dropdown (QComboBox)
        dropdown_box = QtWidgets.QComboBox()
        for i in range(len(option_names)):
            dropdown_box.addItem(option_names[i], userData=option_values[i])
        self.dd_menus[title] = dropdown_box
        self.dd_option_names[title] = option_names
        self.dd_option_values[title] = option_values

        # If a callback function is provided, connect it
        if on_change_callback and ctrl_var:
            dropdown_box.currentIndexChanged.connect(
                lambda idx, t=title, ctrl_v=ctrl_var: on_change_callback(t, ctrl_v, dropdown_box.itemText(idx), dropdown_box.itemData(idx))
            )
        elif on_change_callback and not ctrl_var:
            dropdown_box.currentIndexChanged.connect(
                lambda idx, t=title: on_change_callback(t, dropdown_box.itemText(idx), dropdown_box.itemData(idx))
            )

        # Add button to vertical container
        container.addWidget(dropdown_box)

        # Wrap the layout in a QWidget and add it to the grid
        container_widget = QtWidgets.QWidget()
        container_widget.setLayout(container)
        container_widget.setMaximumWidth(500)  # prevent stretching (aesthetics)
        self.layout.addWidget(container_widget, row, col)

    # Change the buffer size of multiple plots at once, intended to be attached to a dropdown menu.
    # ctrl_plot_ids is a list of plot_ids that correspond to the plots to change.
    def change_buffer_size_multiple(self, title, ctrl_plot_ids, dropdown_text, new_option_value):
        for plot_id in ctrl_plot_ids:
            self.plots[str(plot_id)].buffer_size = new_option_value


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
    '''Read-only, filterable, collapsible log pane at the bottom of the window.
    Every line is also mirrored to a timestamped file on disk (flushed on every
    write) so the log survives a GUI crash, same as the data logs it sits next to.'''

    MAX_LINES = 5000

    def __init__(self):
        super().__init__()
        self._lines = []  # formatted strings, capped at MAX_LINES, independent of the active filter

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

        filename = f"event_log_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
        self._file = open(filename, 'a', encoding='utf-8')

    def add_line(self, level, message):
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
        self.plots = {}  # always empty -- duck-types as a tab for _refresh_tab_badges()
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
