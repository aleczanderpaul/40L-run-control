from pyqtgraph.Qt import QtWidgets, QtCore
import pyqtgraph as pg
import sys
import pandas as pd
import subprocess
import threading
import platform
import serial.tools.list_ports

from .get_data_for_GUI import get_n_XY_datapoints
from .models import Channel, Plot, LoggerControl

'''Class to handle live plotting and add various controls/buttons in a Qt GUI application.'''

COLOR_CYCLE = ['y', 'c', 'm', 'r', 'g', 'b', 'w']

LED_COLORS = {'running': '#2ecc71', 'stopped': '#95a5a6', 'crashed': '#e74c3c'}


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

        # Tabs (left) and the persistent control dock (right) sit side by side and stay
        # visible regardless of which tab is selected -- same treatment as the status
        # strip/banner/event log, which live in main_layout above/below this splitter.
        self.tabs = QtWidgets.QTabWidget()
        self.control_dock = ControlDock(self)

        self.main_splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self.main_splitter.addWidget(self.tabs)
        self.main_splitter.addWidget(self.control_dock)
        self.main_splitter.setStretchFactor(0, 4)
        self.main_splitter.setStretchFactor(1, 1)
        self.main_layout.addWidget(self.main_splitter)

        self.tab_objects = {}  # tab_name -> LiveTab object
        self.channels = {}     # channel_id -> Channel, registered once, shared across all tabs

        # Calls the cleanup function when the application is about to quit so that all running subprocesses are terminated
        self.app.aboutToQuit.connect(self.cleanup)

    # Register a data source once so it can be referenced by id from any tab's plots,
    # read for the status strip, or evaluated for alarms -- with or without a plot.
    def add_channel(self, id, label, long_label, filepath, datatype, units, log_interval_s, alarm=None, vmm_num=None):
        channel = Channel(
            id=id, label=label, long_label=long_label, filepath=filepath, datatype=datatype,
            units=units, log_interval_s=log_interval_s, alarm=alarm, vmm_num=vmm_num,
        )
        self.channels[id] = channel
        return channel

    # Create a tab in the window to put plots and buttons in
    def create_tab(self, tab_name, plots_per_row):
        tab = LiveTab(plots_per_row, plotter=self)
        self.tab_objects[tab_name] = tab
        self.tabs.addTab(tab, tab_name)
        return tab

    # End all running logger subprocesses
    def cleanup(self):
        self.control_dock.cleanup()

    # Show the window and start the event loop
    def run(self):
        self.main_window.show()
        sys.exit(self.app.exec_())


class LiveTab(QtWidgets.QWidget):
    def __init__(self, plots_per_row, plotter):
        super().__init__()  # Call the constructor of the parent class (QWidget) to properly initialize the widget. This class is now a custom QTWidget

        self.plotter = plotter  # back-reference, needed to resolve channel ids

        # Create a scroll area
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)

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

    # Add a new plot with button below it
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

        # Vertical layout to hold the plot and button
        container = QtWidgets.QVBoxLayout()

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

        self.plots[plot_id] = plot

        # Create the start/stop button
        start_stop_button = QtWidgets.QPushButton(f"Stop {title}")
        start_stop_button.setStyleSheet("background-color: red;")
        start_stop_button.clicked.connect(lambda _, p=plot_id: self.toggle_plot(p))
        plot.start_stop_button = start_stop_button

        # Add plot and button to vertical container
        container.addWidget(plot_widget)
        container.addWidget(start_stop_button)

        # Wrap the layout in a QWidget and add it to the grid
        container_widget = QtWidgets.QWidget()
        container_widget.setLayout(container)
        container_widget.setMinimumSize(25 * 16, 40 * 9)
        self.layout.addWidget(container_widget, row, col)

        return plot

    # Update function: fetches data from file and updates the plot
    def update(self, plot_id):
        plot = self.plots[plot_id]
        for i, channel_id in enumerate(plot.channel_ids):
            channel = self._channel(channel_id)
            offset = plot.offsets[i]

            try:
                x_data, y_data = get_n_XY_datapoints(channel.filepath, plot.buffer_size, channel.datatype, channel.vmm_num)
            except (pd.errors.ParserError, ValueError) as e:
                print(f"[{plot.title}] update failed: {e}")
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

    # Toggle between start and stop for a given plot
    def toggle_plot(self, plot_id):
        plot = self.plots[plot_id]
        if plot.running:
            # Stop the timer and update the button text
            plot.interval_timer.stop()
            plot.start_stop_button.setText(f"Start {plot.title}")
            plot.start_stop_button.setStyleSheet("background-color: green;")
            plot.running = False
        else:
            # Restart updates
            plot.elapsed_timer.restart()
            plot.interval_timer.start()
            plot.start_stop_button.setText(f"Stop {plot.title}")
            plot.start_stop_button.setStyleSheet("background-color: red;")
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

    def __init__(self, plotter):
        super().__init__()
        self.plotter = plotter
        self.loggers = {}  # id -> LoggerControl

        self.layout = QtWidgets.QVBoxLayout()
        self.setLayout(self.layout)

        self.loggers_layout = QtWidgets.QVBoxLayout()
        self.layout.addLayout(self.loggers_layout)
        self.layout.addStretch(1)

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
            print(f"[{logger.label}] interval change to {logger.interval_combo.itemText(idx)} will apply on next start")
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

    def _read_stderr(self, logger):
        process = logger.process
        if process.stderr is None:
            return
        for line in process.stderr:
            line = line.rstrip('\n')
            if line:
                logger.stderr_lines.append(line)
                if len(logger.stderr_lines) > 200:
                    logger.stderr_lines.pop(0)
                print(f"[{logger.label} stderr] {line}")  # routed to the real event log in a later commit

    def _stop_logger(self, logger):
        logger.user_stopped = True
        self._kill(logger.process)
        logger.running = False
        logger.start_stop_button.setText(f'Start {logger.label}')
        logger.start_stop_button.setStyleSheet("background-color: green;")
        self._set_led(logger, 'stopped')

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
                    print(f"Logger {logger.label} exited unexpectedly (code {exit_code}): {last_line}")  # routed to the real event log in a later commit

    def cleanup(self):
        for logger in self.loggers.values():
            logger.user_stopped = True
            self._kill(logger.process)
