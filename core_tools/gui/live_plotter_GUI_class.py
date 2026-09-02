from pyqtgraph.Qt import QtWidgets, QtCore
import pyqtgraph as pg
import sys
import pandas as pd
import subprocess
import shlex
import platform

from .get_data_for_GUI import get_n_XY_datapoints
from .models import Channel, Plot

'''Class to handle live plotting and add various controls/buttons in a Qt GUI application.'''

COLOR_CYCLE = ['y', 'c', 'm', 'r', 'g', 'b', 'w']


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

        # Add a tab widget to the main layout
        self.tabs = QtWidgets.QTabWidget()
        self.main_layout.addWidget(self.tabs)

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

    # Call cleanup function for each tab to end all running subprocesses
    def cleanup(self):
        for tab_name in self.tab_objects:
            self.tab_objects[tab_name].cleanup()

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

        # Internal state tracking for command buttons (superseded by add_logger_control soon)
        self.cmd_buttons = {}             # title -> QPushButton for terminal commands
        self.cmd_processes = {}           # title -> subprocess.Popen object for running commands
        self.cmd_running_state = {}       # title -> bool: is command running
        self.cmd_command_strings = {}     # title -> command string (useful if we want to change command on the fly)
        self.cmd_status_timer = None      # QTimer polling check_command_status, held to prevent garbage collection

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

    # Run a terminal command using subprocess
    def run_terminal_command(self, title, command):
        system = platform.system()

        if system == 'Windows':
            # Use shlex.split to safely split the command respecting shell syntax
            cmd_parts = shlex.split(command, posix=False)  # Use posix=False for Windows compatibility
            process = subprocess.Popen(cmd_parts, shell=True)  # Use shell=True for Windows to handle commands correctly
        else:
            cmd_parts = shlex.split(command)
            process = subprocess.Popen(cmd_parts)

        self.cmd_processes[title] = process

    # Terminate a running terminal command
    def stop_terminal_command(self, title):
        process = self.cmd_processes[title]

        # Check if the process is still running and terminate it
        if process and process.poll() is None:
            system = platform.system()
            if system == 'Windows':
                subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], check=True)
            else:
                process.kill()

    # Handle button click for starting/stopping terminal commands
    def cmd_button_clicked(self, title):
        command = self.cmd_command_strings[title]  # this method allows us to dynamically change the command if necessary
        if self.cmd_running_state[title]:
            # If the command is running, stop it
            self.stop_terminal_command(title)

            cmd_button = self.cmd_buttons[title]
            cmd_button.setText(f'Start {title}')
            cmd_button.setStyleSheet("background-color: green;")

            self.cmd_running_state[title] = False
        else:
            # If the command is not running, start it
            self.run_terminal_command(title, command)

            cmd_button = self.cmd_buttons[title]
            cmd_button.setText(f'Stop {title}')
            cmd_button.setStyleSheet("background-color: red;")

            self.cmd_running_state[title] = True

    # Add a button that runs a terminal command on click
    def add_command_button(self, title, command):
        index = self.plot_counts
        plots_per_row = self.plots_per_row
        self.plot_counts += 1
        row = index // plots_per_row
        col = index % plots_per_row

        # Vertical layout to hold the plot and button
        container = QtWidgets.QVBoxLayout()

        # Create button
        cmd_button = QtWidgets.QPushButton(f'Start {title}')
        cmd_button.setStyleSheet("background-color: green;")
        self.cmd_command_strings[title] = command
        cmd_button.clicked.connect(lambda _, t=title: self.cmd_button_clicked(t))
        self.cmd_buttons[title] = cmd_button

        # Add button to vertical container
        container.addWidget(cmd_button)

        # Wrap the layout in a QWidget and add it to the grid
        container_widget = QtWidgets.QWidget()
        container_widget.setLayout(container)
        self.layout.addWidget(container_widget, row, col)

        # Mark the command as not running
        self.cmd_running_state[title] = False

    # Check the status of all command processes and update button states
    def check_command_status(self):
        for title in self.cmd_processes:
            process = self.cmd_processes[title]
            if process.poll() is not None:
                # Process has finished, update button state
                cmd_button = self.cmd_buttons[title]
                cmd_button.setText(f'Start {title}')
                cmd_button.setStyleSheet("background-color: green;")
                self.cmd_running_state[title] = False

    def cmd_timer(self, interval_ms):
        # Create a timer to check command status on a regular interval
        timer = QtCore.QTimer()
        timer.timeout.connect(lambda: self.check_command_status())
        timer.start(interval_ms)
        self.cmd_status_timer = timer  # held to prevent garbage collection

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

    # Changes the command string associated with a specified command button based on the title of the button
    def change_cmd_button_command(self, title, new_command):
        self.cmd_command_strings[title] = new_command

    # Specialized function specifically for the 40L TPC commands, will likely not be useful for a general user of this program
    # People using this program for a different use case will need to edit this function and/or write a new one to do the editing they need to the command string
    def change_cmd(self, title, ctrl_title, dropdown_text, new_option_value):
        old_command = self.cmd_command_strings[ctrl_title]

        parts = old_command.split()
        parts[-1] = str(new_option_value)

        new_command = ' '.join(parts)

        print(f'New Command: {new_command}')  # for debugging

        self.change_cmd_button_command(ctrl_title, new_command)

    # Change the buffer size of multiple plots at once, intended to be attached to a dropdown menu.
    # ctrl_plot_ids is a list of plot_ids that correspond to the plots to change.
    def change_buffer_size_multiple(self, title, ctrl_plot_ids, dropdown_text, new_option_value):
        for plot_id in ctrl_plot_ids:
            self.plots[str(plot_id)].buffer_size = new_option_value

    # End all running subprocesses
    def cleanup(self):
        for title in self.cmd_processes:
            process = self.cmd_processes[title]
            if process.poll() is None:
                self.stop_terminal_command(title)
