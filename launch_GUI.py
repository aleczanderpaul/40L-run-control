from core_tools.gui.live_plotter_GUI_class import LivePlotter
from core_tools.MKSPDR2000_pressure.save_pressure_readings_functions import create_pressure_log_csv
from core_tools.flowrate.save_gas_flow_readings_functions import create_flow_log_csv

'''Launches run control GUI for the 40L system as specified by the user in this file.'''
#Create the CSV files for logging data BEFORE adding the relevant plot to the GUI window because the plotter will look for the file when it is created. Use the create_X_log_csv functions to create the files.
#Do NOT use any filenames with whitespaces in them, as this will cause issues with the terminal command buttons.
#The widgets (plots, buttons, etc.) are added to the GUI window in the order they are written here and fill from left to right, top to bottom.
#For more infromation on how to use the LivePlotter class, see GitHub readme file or the source code at core_tools/gui/live_plotter_GUI_class.py

plotter = LivePlotter("Test Live Plotter")

outer_vessel_pressure_log_filepath = 'outer_vessel_pressure_log.csv'
inner_vessel_pressure_log_filepath = 'inner_vessel_pressure_log.csv'
gas_flow_log_filepath = 'gas_flow_log.csv'

inner_vessel_pressure_offset = 9.16837220249
outer_vessel_pressure_g1_offset = 0
outer_vessel_pressure_g2_offset = 0
gas_flow_offset = 0

create_pressure_log_csv(outer_vessel_pressure_log_filepath)
create_flow_log_csv(gas_flow_log_filepath)

pressure_tab = plotter.create_tab(tab_name='Pressure', plots_per_row=2)

interval_time_ms = 1000

#pressure tab plots
inner_vessel_plot_title = 'Plot Inner Vessel Pressure'
pressure_tab.add_plot(title=inner_vessel_plot_title, x_axis=('Time since present', 's'), y_axis=('Pressure', 'Torr'), offset=[inner_vessel_pressure_offset], buffer_size=10, csv_filepaths=[inner_vessel_pressure_log_filepath], datatypes=['inner_vessel_pressure'])
pressure_tab.start_timer(title=inner_vessel_plot_title, interval_ms=interval_time_ms)

outer_vessel_plot_title = 'Plot Outer Vessel Pressure (g1=yellow & g2=cyan)'
pressure_tab.add_plot(title=outer_vessel_plot_title, x_axis=('Time since present', 's'), y_axis=('Pressure', 'Torr'), offset=[outer_vessel_pressure_g1_offset, outer_vessel_pressure_g2_offset], buffer_size=10, csv_filepaths=[outer_vessel_pressure_log_filepath, outer_vessel_pressure_log_filepath], datatypes=['outer_vessel_gauge_1_pressure', 'outer_vessel_gauge_2_pressure'])
pressure_tab.start_timer(title=outer_vessel_plot_title, interval_ms=interval_time_ms)

both_vessels_plot_title = 'Plot Inner (purple) & Outer (g1=yellow & g2=cyan) Vessel Pressure'
pressure_tab.add_plot(title=both_vessels_plot_title, x_axis=('Time since present', 's'), y_axis=('Pressure', 'Torr'), offset=[outer_vessel_pressure_g1_offset, outer_vessel_pressure_g2_offset, inner_vessel_pressure_offset], buffer_size=10, csv_filepaths=[outer_vessel_pressure_log_filepath, outer_vessel_pressure_log_filepath, inner_vessel_pressure_log_filepath], datatypes=['outer_vessel_gauge_1_pressure', 'outer_vessel_gauge_2_pressure', 'inner_vessel_pressure'])
pressure_tab.start_timer(title=both_vessels_plot_title, interval_ms=interval_time_ms)

gas_flowrate_plot_title = 'Plot Gas Flowrate'
pressure_tab.add_plot(title=gas_flowrate_plot_title, x_axis=('Time since present', 's'), y_axis=('Flowrate', 'L/min'), offset=[gas_flow_offset], buffer_size=10, csv_filepaths=[gas_flow_log_filepath], datatypes=['flowrate'])
pressure_tab.start_timer(title=gas_flowrate_plot_title, interval_ms=interval_time_ms)

#pressure tab controls
pressure_tab.add_dropdown_menu(title='Pressure log increment', option_names=['2s', '10s', '1m', '10m', '1hr'], option_values=[2, 10, 60, 600, 600*6], ctrl_var='Log Outer Vessel Pressure', on_change_callback=pressure_tab.change_pressure_or_flowrate_cmd)
pressure_tab.add_command_button(title='Log Outer Vessel Pressure', command=f'.venv\Scripts\python.exe log_pressure.py {outer_vessel_pressure_log_filepath} COM3 2')

pressure_tab.add_dropdown_menu(title='Gas flowrate log increment', option_names=['2s', '10s', '1m', '10m', '1hr'], option_values=[2, 10, 60, 600, 600*6], ctrl_var='Log Gas Flowrate', on_change_callback=pressure_tab.change_pressure_or_flowrate_cmd)
pressure_tab.add_command_button(title='Log Gas Flowrate', command=f'.venv\Scripts\python.exe log_gas_flowrate.py {gas_flow_log_filepath} COM5 2')

pressure_tab.add_dropdown_menu(title='Gas Flowrate Setting', option_names=['0%', '5%', '25%', '50%', '75%', '100%'], option_values=[0, 5, 25, 50, 75, 100], ctrl_var='Change Gas Flowrate', on_change_callback=pressure_tab.change_pressure_or_flowrate_cmd)
pressure_tab.add_command_button(title='Change Gas Flowrate', command=f'.venv\Scripts\python.exe change_gas_flowrate.py COM5 0')

pressure_ctrl_titles = [inner_vessel_plot_title, outer_vessel_plot_title, both_vessels_plot_title, gas_flowrate_plot_title]
pressure_tab.add_dropdown_menu(title='# data points shown', option_names=['10', '50', '100', '1000', '10000', '100000'], option_values=[10, 50, 100, 1000, 10000, 100000], ctrl_var=pressure_ctrl_titles, on_change_callback=pressure_tab.change_buffer_size_multiple)


pressure_tab.cmd_timer(100)

'''temp_tab = plotter.create_tab(tab_name='Temperature', plots_per_row=4)
num_vmms = 32
temp_ctrl_titles = []
for i in range(0, num_vmms):
    temp_tab.add_plot(title=f'Plot VMM {i} Temperature', x_axis=('Time since present', 's'), y_axis=('Temperature', 'deg C'), buffer_size=10, csv_filepaths=[pressure_log_filepath], datatypes=['outer_vessel_pressure'])
    temp_tab.start_timer(title=f'Plot VMM {i} Temperature', interval_ms=interval_time_ms)
    temp_ctrl_titles.append(f'Plot VMM {i} Temperature')
temp_tab.add_dropdown_menu(title='# data points shown', option_names=['10', '50', '100', '1000', '10000'], option_values=[10, 50, 100, 1000, 10000], ctrl_var=temp_ctrl_titles, on_change_callback=temp_tab.change_buffer_size_multiple)
'''

plotter.run()