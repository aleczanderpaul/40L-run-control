from core_tools.gui.live_plotter_GUI_class import LivePlotter
from core_tools.MKSPDR2000_pressure.save_pressure_readings_functions import create_pressure_log_csv
from core_tools.VaisalaDMT143_H2Osensor.save_H2O_sensor_readings_functions import create_H2O_log_csv
from core_tools.alarms import AlarmSpec

'''Launches run control GUI for the 40L system as specified by the user in this file.'''
#Create the files for logging data BEFORE registering the relevant channel because the plotter will look for the file when the channel is registered. Use the create_X_log_csv functions to create the files.
#Do NOT use any filenames with whitespaces in them, as this will cause issues with the terminal command buttons.
#The widgets (plots, buttons, etc.) are added to the GUI window in the order they are written here and fill from left to right, top to bottom.
#For more infromation on how to use the LivePlotter class, see source code at core_tools/gui/live_plotter_GUI_class.py

plotter = LivePlotter("40L Run Control")

outer_vessel_pressure_log_filepath = 'outer_vessel_pressure_log.csv'
gauge_pressure_log_filepath = 'gauge_pressure_log_0.dat'
alicat_flow_pressure_temp_log_filepath = 'alicat_flow_pressure_temp_log.csv'
vaisala_H2O_log_filepath = 'H2O_log.csv'
vmm_temperatures_log_filepath = 'vmm_temperatures.csv'

outer_vessel_pressure_g1_offset = 0
outer_vessel_pressure_g2_offset = 0
gauge_pressure_offset = 0
filter_line_gas_flow_offset = 0
filter_line_pressure_offset = 0
filter_line_temperature_offset = 0
filter_line_H2O_offset = 0
vmm_temperatures_offset = 0

interval_time_ms = 1000
num_vmms = 16

create_pressure_log_csv(outer_vessel_pressure_log_filepath)
create_H2O_log_csv(vaisala_H2O_log_filepath)

'''CHANNELS -- each data source is declared once, independent of which plot (if any) displays it.
Alarm thresholds live here; see core_tools/alarms.py for what each AlarmSpec field means.'''
plotter.add_channel(id='ov_pressure_g1', label='OV g1', long_label='Outer Vessel Gauge 1 Pressure',
                     filepath=outer_vessel_pressure_log_filepath, datatype='outer_vessel_gauge_1_pressure',
                     units='Torr', log_interval_s=2, alarm=AlarmSpec(high=760.0, clear_high=750.0))
plotter.add_channel(id='ov_pressure_g2', label='OV g2', long_label='Outer Vessel Gauge 2 Pressure',
                     filepath=outer_vessel_pressure_log_filepath, datatype='outer_vessel_gauge_2_pressure',
                     units='Torr', log_interval_s=2, alarm=AlarmSpec(high=760.0, clear_high=750.0))
#The gauge pressure sensor is +/-5V mapped to +/-5 Torr, so a *saturated* sensor reads exactly
#5.00 and will not trip a strict `> 5.0` test. This limit intentionally detects impossible
#readings (a wiring/scaling fault), not saturation -- do not change the comparison to `>=`.
plotter.add_channel(id='gauge_pressure', label='Gauge', long_label='Gauge Pressure',
                     filepath=gauge_pressure_log_filepath, datatype='gauge_pressure',
                     units='Torr', log_interval_s=0.5, alarm=AlarmSpec(abs_high=5.0, clear_abs_high=4.5))
plotter.add_channel(id='filter_line_flow', label='Flow', long_label='Filter Line Gas Flowrate',
                     filepath=alicat_flow_pressure_temp_log_filepath, datatype='filter_line_flowrate',
                     units='SLM', log_interval_s=1)
plotter.add_channel(id='filter_line_pressure', label='FL Press', long_label='Filter Line Pressure',
                     filepath=alicat_flow_pressure_temp_log_filepath, datatype='filter_line_pressure',
                     units='Torr', log_interval_s=1)
plotter.add_channel(id='filter_line_temperature', label='FL Temp', long_label='Filter Line Temperature',
                     filepath=alicat_flow_pressure_temp_log_filepath, datatype='filter_line_temperature',
                     units='degC', log_interval_s=1)
plotter.add_channel(id='filter_line_h2o', label='H2O', long_label='Filter Line H2O Concentration',
                     filepath=vaisala_H2O_log_filepath, datatype='filter_line_H2O_concentration',
                     units='ppm', log_interval_s=2)
for i in range(num_vmms):
    plotter.add_channel(id=f'vmm_temp_{i}', label=f'VMM {i}', long_label=f'VMM {i} Temperature',
                         filepath=vmm_temperatures_log_filepath, datatype='vmm_temperature',
                         units='degC', log_interval_s=2, alarm=AlarmSpec(high=40.0, clear_high=38.0), vmm_num=i)

'''GAS SYSTEM TAB'''
pressure_tab = plotter.create_tab(tab_name='Gas System', plots_per_row=2)

#gas system tab plots
outer_vessel_plot_title = 'Outer Vessel Pressure'
pressure_tab.add_plot(plot_id='ov_pressure', title=outer_vessel_plot_title, channels=['ov_pressure_g1', 'ov_pressure_g2'],
                       x_axis=('Time since present', 's'), y_axis=('Pressure', 'Torr'),
                       offsets=[outer_vessel_pressure_g1_offset, outer_vessel_pressure_g2_offset],
                       buffer_size=10, group='Outer Vessel')
pressure_tab.start_timer(plot_id='ov_pressure', interval_ms=interval_time_ms)

gauge_pressure_plot_title = 'Gauge Pressure'
pressure_tab.add_plot(plot_id='gauge_pressure', title=gauge_pressure_plot_title, channels=['gauge_pressure'],
                       x_axis=('Time since present', 's'), y_axis=('Pressure', 'Torr'),
                       offsets=[gauge_pressure_offset], buffer_size=10, group='Outer Vessel')
pressure_tab.start_timer(plot_id='gauge_pressure', interval_ms=interval_time_ms)

filter_line_gas_flow_plot_title = 'Filter Line Gas Flowrate'
pressure_tab.add_plot(plot_id='filter_line_flow', title=filter_line_gas_flow_plot_title, channels=['filter_line_flow'],
                       x_axis=('Time since present', 's'), y_axis=('Flowrate', 'SLM'),
                       offsets=[filter_line_gas_flow_offset], buffer_size=10, group='Filter Line')
pressure_tab.start_timer(plot_id='filter_line_flow', interval_ms=interval_time_ms)

filter_line_pressure_plot_title = 'Filter Line Pressure'
pressure_tab.add_plot(plot_id='filter_line_pressure', title=filter_line_pressure_plot_title, channels=['filter_line_pressure'],
                       x_axis=('Time since present', 's'), y_axis=('Pressure', 'Torr'),
                       offsets=[filter_line_pressure_offset], buffer_size=10, group='Filter Line')
pressure_tab.start_timer(plot_id='filter_line_pressure', interval_ms=interval_time_ms)

filter_line_temperature_plot_title = 'Filter Line Temperature'
pressure_tab.add_plot(plot_id='filter_line_temperature', title=filter_line_temperature_plot_title, channels=['filter_line_temperature'],
                       x_axis=('Time since present', 's'), y_axis=('Temperature', 'degC'),
                       offsets=[filter_line_temperature_offset], buffer_size=10, group='Filter Line')
pressure_tab.start_timer(plot_id='filter_line_temperature', interval_ms=interval_time_ms)

filter_line_H2O_plot_title = 'Filter Line H2O Concentration'
pressure_tab.add_plot(plot_id='filter_line_h2o', title=filter_line_H2O_plot_title, channels=['filter_line_h2o'],
                       x_axis=('Time since present', 's'), y_axis=('Concentration', 'ppm'),
                       offsets=[filter_line_H2O_offset], buffer_size=10, group='Filter Line')
pressure_tab.start_timer(plot_id='filter_line_h2o', interval_ms=interval_time_ms)

#pressure tab controls
pressure_ctrl_plot_ids = ['ov_pressure', 'gauge_pressure', 'filter_line_flow', 'filter_line_pressure', 'filter_line_temperature', 'filter_line_h2o']

pressure_tab.add_dropdown_menu(title='Pressure log increment', option_names=['2s', '10s', '1m', '10m', '1hr'], option_values=[2, 10, 60, 600, 3600], ctrl_var='Log Outer Vessel Pressure', on_change_callback=pressure_tab.change_cmd)
pressure_tab.add_dropdown_menu(title='H2O concentration log increment', option_names=['2s', '10s', '1m', '10m', '1hr'], option_values=[2, 10, 60, 600, 3600], ctrl_var='Log H2O Concentration', on_change_callback=pressure_tab.change_cmd)

pressure_tab.add_command_button(title='Log Outer Vessel Pressure', command=f'python log_pressure.py {outer_vessel_pressure_log_filepath} COM3 2')
pressure_tab.add_command_button(title='Log H2O Concentration', command=f'python log_H2O_readings.py {vaisala_H2O_log_filepath} COM7 2')

pressure_tab.add_dropdown_menu(title='# data points shown', option_names=['10', '50', '100', '1000', '10000', '100000'], option_values=[10, 50, 100, 1000, 10000, 100000], ctrl_var=pressure_ctrl_plot_ids, on_change_callback=pressure_tab.change_buffer_size_multiple)

pressure_tab.cmd_timer(100)

'''VMM TEMPERATURES TAB'''
temp_tab = plotter.create_tab(tab_name='VMM Temperatures', plots_per_row=4)

#VMM temperature tab plots
vmm_plot_ids = []
for i in range(num_vmms):
    plot_id = f'vmm_temp_{i}'
    temp_tab.add_plot(plot_id=plot_id, title=f'VMM {i} Temperature', channels=[plot_id],
                       x_axis=('Time since present', 's'), y_axis=('Temperature', 'degC'),
                       offsets=[vmm_temperatures_offset], buffer_size=10)
    temp_tab.start_timer(plot_id=plot_id, interval_ms=interval_time_ms)
    vmm_plot_ids.append(plot_id)
temp_tab.add_dropdown_menu(title='# data points shown', option_names=['10', '50', '100', '1000', '10000'], option_values=[10, 50, 100, 1000, 10000], ctrl_var=vmm_plot_ids, on_change_callback=temp_tab.change_buffer_size_multiple)

plotter.run()
