# 40L-Run-Control

Repository of code for a run control program for the 40L TPC. Intended to measure/plot vessel pressure and VMM temperature in real time, and to control the experiment remotely.

NOT FULLY UPDATED YET, SOME FEATURES MAY NOT BE ADDED HERE AT THE MOMENT

## launch_GUI.py

A script to launch the run control GUI. The widgets (plots, buttons, etc.) to display can be specified in the file using functions in the LivePlotter class (from core_tools/gui/live_plotter_GUI_class.py). Some notes about how to use launch_GUI.py are left as comments inside the file.

## log_pressure.py

A script that connects to an MKS PDR 2000 (pressure sensor that uses RS-232 Serial protocol) and writes the pressure to a CSV file at a specified interval indefinitely or for a limited duration.

To run script, use format: python3 <log_pressure.py filepath> <log_filepath (make sure to add .csv)> <serial_port> <interval_sec> <duration_sec (optional, leave empty for indefinite)>

If using venv, use format: .venv\Scripts\python.exe <log_pressure.py filepath> <log_filepath (make sure to add .csv)> <serial_port> <interval_sec> <duration_sec (optional, leave empty for indefinite)>

While technically the user can create the CSV file manually and the script will skip making one if it already exists, it is highly recommended that the user lets the script make the file, as it will make the headers for each column correctly for the GUI to read from.

## log_temperature.py

TO BE DEVELOPED

## LivePlotter class

When called, an object of this class will launch a window that will later be filled with tabs to form a GUI.

All the functions inside the class are explained below, but the only ones that should be called in launch_GUI.py are create_tab and run.

Source code is located at core_tools/gui/live_plotter_GUI_class.py.

### create_tab(tab_name, plots_per_row)

Creates a "tab" inside the GUI window for the user to switch between. Helps organize different sets of plots instead of all on the same page all the time.

tab_name is a string that tells the GUI what to name the tab.

plots_per_row is an int that tells the GUI how many plots to put into each row before moving onto the next one, can vary this number for each tab.

This function returns a LiveTab object, which is a class with functions described in the next section. In order to add widgets like plots and buttons to each tab, use the functions in the LiveTab class. For example:

```python
plotter = LivePlotter("Test Live Plotter")
tab_1 = plotter.create_tab(tab_name='Test Tab', plots_per_row=1)
tab_1.add_plot(title='Plot Vessel Pressure', x_axis=('Time since present', 's'), y_axis=('Pressure', 'Torr'), buffer_size=100, csv_filepath=pressure_log_filepath, datatype='pressure')
```

This snippet of code will create a GUI window, add a tab, and add a plot to the tab that logs pressure vs time. The specifics of the add_plot function are explained in the LiveTab class documentation.

### cleanup()

Terminates all the running subprocesses the GUI started (e.g., logging pressure script). Is called when the user exits the GUI.

### run()

Shows the window and starts the event loop. Call this after all the widgets have been added to the window.

## LiveTab class

When called, an object of this class will launch a tab widget inside the LivePlotter window that can be filled with other widgets like plots and buttons.

This class uses dictionaries to store and sort data, so titles are a very important concept for this class. Every widget has a title specified by the user and can be any string, but must be unique across all widgets. The title lets the GUI know where to store important processes for the widgets (e.g., plot data, timers, filepaths, etc.) so that each one can run independently, and can be accessed later.

All the functions inside the class are explained below, but the only ones that should be called in launch_GUI.py are add_plot, start_timer, add_command_button, and cmd_timer.

Source code is located at core_tools/gui/live_plotter_GUI_class.py.

### add_plot(title, x_axis, y_axis, buffer_size, csv_filepath, datatype)

Adds a plot to the window and a button that will start/stop automatic updates to the plot. Data is pulled from a CSV file, so the CSV must exist before this function is called, even if it is empty. It is highly recommended to use log_pressure.py and log_temperature.py to create the CSV's, not manually.

x_axis and y_axis are tuples of format (label, unit). For example, x_axis = ('Time', 's') means the x-axis label is Time, and the units are s (seconds). pyqtgraph handles metric prefixes automatically, so there is no need to refactor all your data to be in ms, the program will plot in units that are "smart" to plot in.

buffer_size is an int and represents the number of data points the plot will display at a single time. This is to save memory and to not be an eyesore, so don't set this number egregiously high.

csv_filepath is a string of the filepath to the CSV the plot will pull data from.

datatype is a string that tells the GUI what is being plotted so it knows how to get the relevant x and y data. For example, datatype='pressure' tells the GUI to plot pressure from the MKS PDR 2000 vs how many seconds ago the data was taken. The current supported datatypes are found in core_tools/gui/get_data_for_GUI.py inside the get_n_XY_datapoints function.

### update(title)

Fetches the data from the CSV and updates the plot accordingly. If there is less data in the CSV than the buffer size of the plot, it will plot what is available. If there is more data in the CSV than the buffer size, it will plot data only from the bottom rows of the CSV up to the buffer size. This function is usually fired on a timer so that the plots update constantly (see below sections for more information).

### get_elapsed_time(title)

Return elapsed time in seconds since the plot has started. Using the start/stop button associated with the plot will reset this timer.

### start_timer(title, interval_ms)

Starts the interval timer to drive plot updates and the elapsed timer. Run this line after each add_plot function call, otherwise the plot will never be updated.

interval_ms is an int that specifies the length of the interval timer that calls the update function.

### toggle_plot(title)

Handles the start/stop button for each plot. Changes color, text, and state of the timers when button is pressed.

### run_terminal_command(title, command)

Runs a command in the terminal, to be used in conjunction with a button. Works for both Linux and Windows.

command is a string of the command to be run.

### stop_terminal_command(title)

Terminates a running command, to be used in conjunction with a button. Works for both Linux and Windows.

### cmd_button_clicked(title, command)

Similar to toggle_plot, but handles the buttons that execute terminal commands instead of starting/stopping plot updates.

### add_command_button(title, command)

Adds a button that runs a terminal command on click.

command is a string of the command to be run.

### check_command_status()

Checks the status of all terminal processes associated with a button and reverts the button(s) back to their original state if the process is resolved.

### cmd_timer(interval_ms)

Creates an interval timer that calls check_command_status. This function should be called once after all the command buttons have been added to the window.

interval_ms is an int that specifies the length of the interval timer that calls the check_command_status function.

### add_dropdown_menu(title, option_names, option_values, ctrl_var=None, on_change_callback=None)

Adds a dropdown menu that can call a function upon changing the option selected.

option_names is a list of strings that are the name of each option in the dropdown menu to display in the GUI.

option_values is a list that contains the underlying "value" (int, string, float, etc.) that corresponds to each option name. For example, if a dropdown option is named "1 second", the value for that option may be 1.0, which is used to change an interval timer.

ctrl_var is anything else that the user wants to be passed to the function that is called when the dropdown menu option is changed (list, dict, int, str, float, etc.).

on_change_callback is the function that gets called when the dropdown menu option is changed.

### change_cmd_button_command(title, new_command)

Changes the command string associated with a specified command button based on the title of the button.

new_command is a string and is the new command to be associated with a command button (e.g., 'sleep 10').

### change_pressure_log_cmd(title, ctrl_title, dropdown_text, new_option_value)

Specialized function specifically for the 40L TPC log pressure command, will likely not be useful for a general user of this program.

### change_buffer_size(title, ctrl_title, dropdown_text, new_option_value)

Change the buffer size of a plot to display more or less data points. Intended to be attached to a dropdown menu.

title and dropdown_text do not matter, they are only passed through this function as a consequence of intended for the on_change_callback function of the dropdown menus.

ctrl_title is a string that is the title of the lot whose buffer is to be changed.

new_option value is an int that is the new buffer size for the plot.

### change_buffer_size_multiple(title, ctrl_titles, dropdown_text, new_option_value)

Same as change_buffer_size, but used for changing multiple plots at once.

ctrl_title is now a list of strings of the titles for each plot whose buffer size it to be changed.

### cleanup()

Terminates all the running subprocesses the tab widget started (e.g., logging pressure script). Is called by LivePlotter object when window is closed.
