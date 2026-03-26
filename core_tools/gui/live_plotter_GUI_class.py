from pyqtgraph.Qt import QtWidgets, QtCore
import pyqtgraph as pg
import numpy as np
import sys
import pandas as pd
from .get_data_for_GUI import get_n_XY_datapoints
import subprocess
import shlex
import platform

'''Class to handle live plotting and add various controls/buttons in a Qt GUI application.'''

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

        #Calls the clanup function when the application is about to quit so that all running subprocesses are terminated
        self.app.aboutToQuit.connect(self.cleanup)

    #Create a tab in the window to put plots and buttons in
    def create_tab(self, tab_name, plots_per_row):
        tab = LiveTab(plots_per_row)
        self.tab_objects[tab_name] = tab
        self.tabs.addTab(tab, tab_name)
        return tab

    #Call cleanup function for each tab to end all running subprocesses
    def cleanup(self):
        for tab_name in self.tab_objects:
            self.tab_objects[tab_name].cleanup()
    
    # Show the window and start the event loop
    def run(self):
        self.main_window.show()
        sys.exit(self.app.exec_())

class LiveTab(QtWidgets.QWidget):
    def __init__(self, plots_per_row):
        super().__init__() # Call the constructor of the parent class (QWidget) to properly initialize the widget. This class is now a custom QTWidget

        '''self.layout = QtWidgets.QGridLayout() ## Create a grid layout manager to arrange child widgets (plots, buttons) in a grid format.
        self.setLayout(self.layout)'''

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

        # Internal state tracking for plots
        self.data = {}                            # title -> {x: pandas Series, y: pandas Series, buffer_size: int} (list)
        self.offsets = {}
        self.curves = {}                          # title -> plot curves (list)
        self.interval_timers = {}                 # title -> QTimer for updates
        self.elapsed_timers = {}                  # title -> QElapsedTimer for time axis
        self.running_state = {}                   # title -> bool: is plot running
        self.start_stop_buttons = {}              # title -> start/stop QPushButton
        self.csv_filepaths = {}                    # title -> CSV filepaths from logging to pull data from (list)
        self.datatype = {}                        # Datatype for the plots (e.g., 'pressure', 'temperature')

        #Internal state tracking for command buttons
        self.cmd_buttons = {}                     # title -> QPushButton for terminal commands
        self.cmd_processes = {}                   # title -> subprocess.Popen object for running commands
        self.cmd_running_state = {}               # title -> bool: is command running
        self.cmd_command_strings = {}             # title -> command string (useful if we want to change command on the fly)

        #Internal state tracking for dropdown menus
        self.dd_menus = {}                        # title ->
        self.dd_option_names = {}                 # title ->
        self.dd_option_values = {}                 # title ->
    
    # Add a new plot with button below it
    def add_plot(self, title, x_axis, y_axis, offset, buffer_size, csv_filepaths, datatypes): #x_axis and y_axis are tuples of (label, unit), and buffer_size is the number of data points to display at once
        index = self.plot_counts
        plots_per_row = self.plots_per_row
        self.plot_counts += 1
        row = index // plots_per_row
        col = index % plots_per_row

        # Vertical layout to hold the plot and button
        container = QtWidgets.QVBoxLayout()

        # Create the plot widget
        plot_widget = pg.PlotWidget(title=title)
        plot_widget.setLabel('bottom', x_axis[0], units=x_axis[1])
        plot_widget.setLabel('left', y_axis[0], units=y_axis[1])
        plot_widget.showGrid(x=True, y=True)

        #Store the filepath of the CSV associated with this plot
        self.csv_filepaths[title] = csv_filepaths

        # Store the datatype for this plot
        self.datatype[title] = datatypes

        num_curves = len(csv_filepaths)
        self.data[title] = []
        self.curves[title] = []
        COLOR_CYCLE = ['y', 'c', 'm', 'r', 'g', 'b', 'w']
        for i in range(0, num_curves):
            # Initialize circular buffers for x and y data
            empty_data = {"x": pd.Series(np.full(buffer_size, np.nan), name='x'), "y": pd.Series(np.full(buffer_size, np.nan), name='y'), "offset":offset[i], "buffer_size": buffer_size}
            self.data[title].append(empty_data)
    
            # Create the plot curves
            color = COLOR_CYCLE[i % len(COLOR_CYCLE)]
            curve = plot_widget.plot(pen=color)
            self.curves[title].append(curve)

        # Create the start/stop button
        start_stop_button = QtWidgets.QPushButton(f"Stop {title}")
        start_stop_button.setStyleSheet("background-color: red;")
        start_stop_button.clicked.connect(lambda _, t=title: self.toggle_plot(t))
        self.start_stop_buttons[title] = start_stop_button

        # Add plot and button to vertical container
        container.addWidget(plot_widget)
        container.addWidget(start_stop_button)

        # Wrap the layout in a QWidget and add it to the grid
        container_widget = QtWidgets.QWidget()
        container_widget.setLayout(container)
        container_widget.setMinimumSize(25*16, 40*9)
        self.layout.addWidget(container_widget, row, col)

    # Update function: fetches data from CSV and updates the plot
    def update(self, title):
        num_curves = len(self.csv_filepaths[title])
        for i in range(0, num_curves):
            x_data, y_data, offset, buffer_size = self.data[title][i]["x"], self.data[title][i]["y"], self.data[title][i]['offset'], self.data[title][i]["buffer_size"]
            csv_filepath = self.csv_filepaths[title][i]
            datatype = self.datatype[title][i]
            x_data, y_data = get_n_XY_datapoints(csv_filepath, buffer_size, datatype)
            self.curves[title][i].setData(x=x_data, y=y_data+float(offset))

    # Return elapsed time in seconds since the plot started
    def get_elapsed_time(self, title):
        return self.elapsed_timers[title].elapsed() / 1000.0 #convert ms to seconds

    # Starts the QTimer that drives the updates for a given plot
    def start_timer(self, title, interval_ms):
        # Create a timer to update the plot regularly
        timer = QtCore.QTimer()
        timer.timeout.connect(lambda: self.update(title))
        timer.start(interval_ms)
        self.interval_timers[title] = timer

        # Start a timer to track elapsed time
        elapsed = QtCore.QElapsedTimer()
        elapsed.start()
        self.elapsed_timers[title] = elapsed

        # Mark the plot as running
        self.running_state[title] = True

    # Toggle between start and stop for a given plot
    def toggle_plot(self, title):
        if self.running_state[title]:
            # Stop the timer and update the button text
            self.interval_timers[title].stop()
            self.start_stop_buttons[title].setText(f"Start {title}")
            self.start_stop_buttons[title].setStyleSheet("background-color: green;")
            self.running_state[title] = False
        else:
            # Reset data and timer, restart updates
            num_curves = len(self.csv_filepaths[title])
            for i in range(0, num_curves):
                buffer_size = self.data[title][i]["buffer_size"]
                offset = self.data[title][i]["offset"]
                self.data[title][i] = {"x": pd.Series(np.full(buffer_size, np.nan), name='x'), "y": pd.Series(np.full(buffer_size, np.nan), name='y'), "offset":offset, "buffer_size": buffer_size}
            self.elapsed_timers[title].restart()
            self.interval_timers[title].start()
            self.start_stop_buttons[title].setText(f"Stop {title}")
            self.start_stop_buttons[title].setStyleSheet("background-color: red;")
            self.running_state[title] = True

    #Run a terminal command using subprocess
    def run_terminal_command(self, title, command):
        system = platform.system()

        if system == 'Windows':
            #Use shlex.split to safely split the command respecting shell syntax
            cmd_parts = shlex.split(command, posix=False)  # Use posix=False for Windows compatibility
            process = subprocess.Popen(cmd_parts, shell=True) #Use shell=True for Windows to handle commands correctly
        else:
            cmd_parts = shlex.split(command)
            process = subprocess.Popen(cmd_parts)
        
        self.cmd_processes[title] = process
    
    #Terminate a running terminal command
    def stop_terminal_command(self, title):
        process = self.cmd_processes[title]

        #Check if the process is still running and terminate it
        if process and process.poll() is None:
            system = platform.system()
            if system == 'Windows':
                subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], check=True)
            else:
                process.kill()
    
    #Handle button click for starting/stopping terminal commands
    def cmd_button_clicked(self, title):
        command = self.cmd_command_strings[title] #this method allows us to dynamically change the command if necessary
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
        cmd_button.setStyleSheet(f"background-color: green;")
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
        self.interval_timers['cmd_timer'] = timer #Store primarily to prevent garbage collection
    
    #Add a dropdown menu with specified options and values attached to the options
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
        container_widget.setMaximumWidth(500) #prevent stretching (aesthetics)
        self.layout.addWidget(container_widget, row, col)

    #Changes the command string associated with a specified command button based on the title of the button
    def change_cmd_button_command(self, title, new_command):
        self.cmd_command_strings[title] = new_command
    
    #Specialized function specifically for the 40L TPC log pressure and/or log/change gas flowrate command, will likely not be useful for a general user of this program
    #People using this program for a different use case will need to edit this function and/or write a new one to do the editing they need to the command string
    def change_pressure_or_flowrate_cmd(self, title, ctrl_title, dropdown_text, new_option_value):
        old_command = self.cmd_command_strings[ctrl_title]

        parts = old_command.split()
        parts[-1] = str(new_option_value)

        new_command = ' '.join(parts)

        print(f'New Command: {new_command}') #for debugging

        self.change_cmd_button_command(ctrl_title, new_command)
    
    #Change the buffer size of a specified plot, intended to be attached to a dropdown menu
    def change_buffer_size(self, title, ctrl_title, dropdown_text, new_option_value):
        num_curves = len(self.csv_filepaths[ctrl_title])
        for i in range(0, num_curves):
            self.data[i][ctrl_title]["buffer_size"] = new_option_value

    #Change the buffer size of multiple plots at once, intended to be attached to a dropdown menu
    #ctrl_titles is a list of titles that correspond to the plots to change
    def change_buffer_size_multiple(self, title, ctrl_titles, dropdown_text, new_option_value):
        for i in range(len(ctrl_titles)):
            num_curves = len(self.csv_filepaths[str(ctrl_titles[i])])
            for l in range(0, num_curves):
                self.data[str(ctrl_titles[i])][l]["buffer_size"] = new_option_value
    
    # End all running subprocesses
    def cleanup(self):
        for title in self.cmd_processes:
            process = self.cmd_processes[title]
            if process.poll() is None:
                self.stop_terminal_command(title)