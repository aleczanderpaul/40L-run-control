from core_tools.gui.live_plotter_GUI_class import LivePlotter
from core_tools.MKSPDR2000_pressure.save_pressure_readings_functions import create_pressure_log_csv

'''Launches run control GUI for the 40L system as specified by the user in this file.'''
#Create the files for logging data BEFORE adding the relevant plot to the GUI window because the plotter will look for the file when it is created. Use the create_X_log_csv functions to create the files.
#Do NOT use any filenames with whitespaces in them, as this will cause issues with the terminal command buttons.
#The widgets (plots, buttons, etc.) are added to the GUI window in the order they are written here and fill from left to right, top to bottom.
#For more infromation on how to use the LivePlotter class, see source code at core_tools/gui/live_plotter_GUI_class.py

plotter = LivePlotter("40L Run Control")

outer_vessel_pressure_log_filepath = 'outer_vessel_pressure_log.csv'
gauge_pressure_log_filepath = 'gauge_pressure_log_0.dat'
alicat_flow_pressure_temp_log_filepath = 'alicat_flow_pressure_temp_log.csv'
vmm_temperatures_log_filepath = 'vmm_temperatures.csv'

outer_vessel_pressure_g1_offset = 0
outer_vessel_pressure_g2_offset = 0
gauge_pressure_offset = 0
filter_line_gas_flow_offset = 0
filter_line_pressure_offset = 0
filter_line_temperature_offset = 0
vmm_temperatures_offset = 0

interval_time_ms = 1000

'''GAS SYSTEM TAB'''
create_pressure_log_csv(outer_vessel_pressure_log_filepath)
pressure_tab = plotter.create_tab(tab_name='Gas System', plots_per_row=2)

#pressure tab plots
outer_vessel_plot_title = 'Outer Vessel Pressure (g1=yellow & g2=cyan)'
pressure_tab.add_plot(title=outer_vessel_plot_title, x_axis=('Time since present', 's'), y_axis=('Pressure', 'Torr'), offset=[outer_vessel_pressure_g1_offset, outer_vessel_pressure_g2_offset], buffer_size=10, data_filepaths=[outer_vessel_pressure_log_filepath, outer_vessel_pressure_log_filepath], datatypes=['outer_vessel_gauge_1_pressure', 'outer_vessel_gauge_2_pressure'])
pressure_tab.start_timer(title=outer_vessel_plot_title, interval_ms=interval_time_ms)

gauge_pressure_plot_title = 'Gauge Pressure'
pressure_tab.add_plot(title=gauge_pressure_plot_title, x_axis=('Time since present', 's'), y_axis=('Pressure', 'Torr'), offset=[gauge_pressure_offset], buffer_size=10, data_filepaths=[gauge_pressure_log_filepath], datatypes=['gauge_pressure'])
pressure_tab.start_timer(title=gauge_pressure_plot_title, interval_ms=interval_time_ms)

filter_line_gas_flow_plot_title = 'Filter Line Gas Flowrate'
pressure_tab.add_plot(title=filter_line_gas_flow_plot_title, x_axis=('Time since present', 's'), y_axis=('Flowrate', 'SL/min'), offset=[filter_line_gas_flow_offset], buffer_size=10, data_filepaths=[alicat_flow_pressure_temp_log_filepath], datatypes=['filter_line_flowrate'])
pressure_tab.start_timer(title=filter_line_gas_flow_plot_title, interval_ms=interval_time_ms)

filter_line_pressure_plot_title = 'Filter Line Pressure'
pressure_tab.add_plot(title=filter_line_pressure_plot_title, x_axis=('Time since present', 's'), y_axis=('Pressure', 'Torr'), offset=[filter_line_pressure_offset], buffer_size=10, data_filepaths=[alicat_flow_pressure_temp_log_filepath], datatypes=['filter_line_pressure'])
pressure_tab.start_timer(title=filter_line_pressure_plot_title, interval_ms=interval_time_ms)

filter_line_temperature_plot_title = 'Filter Line Temperature'
pressure_tab.add_plot(title=filter_line_temperature_plot_title, x_axis=('Time since present', 's'), y_axis=('Temperature', 'deg C'), offset=[filter_line_temperature_offset], buffer_size=10, data_filepaths=[alicat_flow_pressure_temp_log_filepath], datatypes=['filter_line_temperature'])
pressure_tab.start_timer(title=filter_line_temperature_plot_title, interval_ms=interval_time_ms)

#pressure tab controls
pressure_ctrl_titles = [outer_vessel_plot_title, gauge_pressure_plot_title, filter_line_gas_flow_plot_title, filter_line_pressure_plot_title, filter_line_temperature_plot_title]
pressure_tab.add_dropdown_menu(title='# data points shown', option_names=['10', '50', '100', '1000', '10000', '100000'], option_values=[10, 50, 100, 1000, 10000, 100000], ctrl_var=pressure_ctrl_titles, on_change_callback=pressure_tab.change_buffer_size_multiple)

pressure_tab.add_dropdown_menu(title='Pressure log increment', option_names=['2s', '10s', '1m', '10m', '1hr'], option_values=[2, 10, 60, 600, 600*6], ctrl_var='Log Outer Vessel Pressure', on_change_callback=pressure_tab.change_cmd)
pressure_tab.add_command_button(title='Log Outer Vessel Pressure', command=f'.venv/Scripts/python.exe log_pressure.py {outer_vessel_pressure_log_filepath} COM3 2')

pressure_tab.cmd_timer(100)

'''VMM TEMPERATURES TAB'''
temp_tab = plotter.create_tab(tab_name='VMM Temperatures', plots_per_row=4)

#VMM temperature tab plots
num_vmms = 16
temp_ctrl_titles = []
for i in range(0, num_vmms):
    temp_tab.add_plot(title=f'VMM {i} Temperature', x_axis=('Time since present', 's'), y_axis=('Temperature', 'deg C'), offset=[vmm_temperatures_offset], buffer_size=10, data_filepaths=[vmm_temperatures_log_filepath], datatypes=['vmm_temperature'], vmm_nums=[i])
    temp_tab.start_timer(title=f'VMM {i} Temperature', interval_ms=interval_time_ms)
    temp_ctrl_titles.append(f'VMM {i} Temperature')
temp_tab.add_dropdown_menu(title='# data points shown', option_names=['10', '50', '100', '1000', '10000'], option_values=[10, 50, 100, 1000, 10000], ctrl_var=temp_ctrl_titles, on_change_callback=temp_tab.change_buffer_size_multiple)

plotter.run()