from core_tools.gui.live_plotter_GUI_class import LivePlotter
from core_tools.gui.models import AggregateTile
from core_tools.MKSPDR2000_pressure.save_pressure_readings_functions import create_pressure_log_csv
from core_tools.VaisalaDMT143_H2Osensor.save_H2O_sensor_readings_functions import create_H2O_log_csv
from core_tools.alarms import AlarmSpec

'''Launches run control GUI for the 40L system as specified by the user in this file.'''
#Create the files for logging data BEFORE registering the relevant channel because the plotter will look for the file when the channel is registered. Use the create_X_log_csv functions to create the files.
#The widgets (plots, buttons, etc.) are added to the GUI window in the order they are written here and fill from left to right, top to bottom.
#For more infromation on how to use the LivePlotter class, see source code at core_tools/gui/live_plotter_GUI_class.py

plotter = LivePlotter("40L Run Control")

outer_vessel_pressure_log_filepath = 'outer_vessel_pressure_log.csv'
gauge_pressure_log_filepath = 'gauge_pressure_log_0.dat'
alicat_gas_inlet_log_filepath = 'alicat_gas_inlet_log.csv'
alicat_filter_line_log_filepath = 'alicat_filter_line_log.csv'
vaisala_H2O_log_filepath = 'H2O_log.csv'
vmm_temperatures_log_filepath = 'vmm_temperatures.csv'

outer_vessel_pressure_g1_offset = 0
outer_vessel_pressure_g2_offset = 0
gauge_pressure_offset = 0
gas_inlet_flow_offset = 0
gas_inlet_flow_setpoint_offset = 0
gas_inlet_valve_drive_offset = 0
gas_inlet_pressure_offset = 0
gas_inlet_temperature_offset = 0
filter_line_gas_flow_offset = 0
filter_line_pressure_offset = 0
filter_line_temperature_offset = 0
filter_line_H2O_offset = 0

num_vmms = 16

create_pressure_log_csv(outer_vessel_pressure_log_filepath)
create_H2O_log_csv(vaisala_H2O_log_filepath)

'''CHANNELS -- each data source is declared once, independent of which plot (if any) displays it.
Alarm thresholds live here; see core_tools/alarms.py for what each AlarmSpec field means.'''
plotter.add_channel(id='ov_pressure_g1', label='OV g1', long_label='Outer Vessel Gauge 1 Pressure',
                     filepath=outer_vessel_pressure_log_filepath, datatype='outer_vessel_gauge_1_pressure',
                     units='Torr', log_interval_s=2, alarm=AlarmSpec(high=760.0, clear_high=750.0),
                     overview_group='Outer Vessel')
plotter.add_channel(id='ov_pressure_g2', label='OV g2', long_label='Outer Vessel Gauge 2 Pressure',
                     filepath=outer_vessel_pressure_log_filepath, datatype='outer_vessel_gauge_2_pressure',
                     units='Torr', log_interval_s=2, alarm=AlarmSpec(high=760.0, clear_high=750.0),
                     overview_group='Outer Vessel')

#The gauge pressure sensor is +/-5V mapped to +/-5 Torr, so a *saturated* sensor reads exactly
#5.00 and will not trip a strict `> 5.0` test. This limit intentionally detects impossible
#readings (a wiring/scaling fault), not saturation -- do not change the comparison to `>=`.
plotter.add_channel(id='gauge_pressure', label='Gauge', long_label='Gauge Pressure',
                     filepath=gauge_pressure_log_filepath, datatype='gauge_pressure',
                     units='Torr', log_interval_s=1, alarm=AlarmSpec(abs_high=5.0, clear_abs_high=4.5),
                     overview_group='Outer Vessel')

plotter.add_channel(id='gas_inlet_flow', label='GI Flow', long_label='Gas Inlet Flowrate',
                     filepath=alicat_gas_inlet_log_filepath, datatype='gas_inlet_flowrate',
                     units='SLPM', log_interval_s=2, overview_group='Gas Inlet')
plotter.add_channel(id='gas_inlet_flow_setpoint', label='GI Flow SP', long_label='Gas Inlet Flowrate Setpoint',
                     filepath=alicat_gas_inlet_log_filepath, datatype='gas_inlet_flowrate_setpoint',
                     units='SLPM', log_interval_s=2, overview_group='Gas Inlet')
plotter.add_channel(id='gas_inlet_valve_drive', label='GI Valve Drive', long_label='Gas Inlet Valve Drive',
                     filepath=alicat_gas_inlet_log_filepath, datatype='gas_inlet_valve_drive',
                     units='%', log_interval_s=2, overview_group='Gas Inlet')
plotter.add_channel(id='gas_inlet_pressure', label='GI Press', long_label='Gas Inlet Pressure',
                     filepath=alicat_gas_inlet_log_filepath, datatype='gas_inlet_pressure',
                     units='Torr', log_interval_s=2, overview_group='Gas Inlet')
plotter.add_channel(id='gas_inlet_temperature', label='GI Temp', long_label='Gas Inlet Temperature',
                     filepath=alicat_gas_inlet_log_filepath, datatype='gas_inlet_temperature',
                     units='degC', log_interval_s=2, overview_group='Gas Inlet')

plotter.add_channel(id='filter_line_flow', label='FL Flow', long_label='Filter Line Gas Flowrate',
                     filepath=alicat_filter_line_log_filepath, datatype='filter_line_flowrate',
                     units='SLPM', log_interval_s=2, overview_group='Filter Line')
plotter.add_channel(id='filter_line_pressure', label='FL Press', long_label='Filter Line Pressure',
                     filepath=alicat_filter_line_log_filepath, datatype='filter_line_pressure',
                     units='Torr', log_interval_s=2, overview_group='Filter Line')
plotter.add_channel(id='filter_line_temperature', label='FL Temp', long_label='Filter Line Temperature',
                     filepath=alicat_filter_line_log_filepath, datatype='filter_line_temperature',
                     units='degC', log_interval_s=2, overview_group='Filter Line')
plotter.add_channel(id='filter_line_h2o', label='H2O', long_label='Filter Line H2O Concentration',
                     filepath=vaisala_H2O_log_filepath, datatype='filter_line_H2O_concentration',
                     units='ppm', log_interval_s=2, overview_group='Filter Line')

for i in range(num_vmms):
    plotter.add_channel(id=f'vmm_temp_{i}', label=f'VMM {i}', long_label=f'VMM {i} Temperature',
                         filepath=vmm_temperatures_log_filepath, datatype='vmm_temperature',
                         units='degC', log_interval_s=2, alarm=AlarmSpec(high=40.0, clear_high=38.0), vmm_num=i,
                         overview_group='VMM Temperatures')

'''OVERVIEW TAB -- built first so it lands first in tab order; reflects every
channel registered above via each one's overview_group.'''
plotter.build_overview_tab()

'''GAS SYSTEM TAB'''
pressure_tab = plotter.create_tab(tab_name='Gas System', plots_per_row=2)

#gas system tab plots -- how much history each shows is governed by the global
#time-window selector in the control dock (§7), not a per-plot setting; all plots
#redraw from LivePlotter's single scan tick, no per-plot timer to start
outer_vessel_plot_title = 'Outer Vessel Pressure'
pressure_tab.add_plot(plot_id='ov_pressure', title=outer_vessel_plot_title, channels=['ov_pressure_g1', 'ov_pressure_g2'],
                       x_axis=('Time since present', 's'), y_axis=('Pressure', 'Torr'),
                       offsets=[outer_vessel_pressure_g1_offset, outer_vessel_pressure_g2_offset],
                       group='Outer Vessel')

gauge_pressure_plot_title = 'Gauge Pressure'
pressure_tab.add_plot(plot_id='gauge_pressure', title=gauge_pressure_plot_title, channels=['gauge_pressure'],
                       x_axis=('Time since present', 's'), y_axis=('Pressure', 'Torr'),
                       offsets=[gauge_pressure_offset], group='Outer Vessel')

gas_inlet_flow_plot_title = 'Gas Inlet Flowrate'
pressure_tab.add_plot(plot_id='gas_inlet_flow', title=gas_inlet_flow_plot_title, channels=['gas_inlet_flow'],
                       x_axis=('Time since present', 's'), y_axis=('Flowrate', 'SLPM'),
                       offsets=[gas_inlet_flow_offset], group='Gas Inlet')

gas_inlet_flow_setpoint_plot_title = 'Gas Inlet Flowrate Setpoint'
pressure_tab.add_plot(plot_id='gas_inlet_flow_setpoint', title=gas_inlet_flow_setpoint_plot_title, channels=['gas_inlet_flow_setpoint'],
                       x_axis=('Time since present', 's'), y_axis=('Flowrate', 'SLPM'),
                       offsets=[gas_inlet_flow_setpoint_offset], group='Gas Inlet')

gas_inlet_valve_drive_plot_title = 'Gas Inlet Valve Drive'
pressure_tab.add_plot(plot_id='gas_inlet_valve_drive', title=gas_inlet_valve_drive_plot_title, channels=['gas_inlet_valve_drive'],
                       x_axis=('Time since present', 's'), y_axis=('Valve Drive', '%'),
                       offsets=[gas_inlet_valve_drive_offset], group='Gas Inlet')

gas_inlet_pressure_plot_title = 'Gas Inlet Pressure'
pressure_tab.add_plot(plot_id='gas_inlet_pressure', title=gas_inlet_pressure_plot_title, channels=['gas_inlet_pressure'],
                       x_axis=('Time since present', 's'), y_axis=('Pressure', 'Torr'),
                       offsets=[gas_inlet_pressure_offset], group='Gas Inlet')

gas_inlet_temperature_plot_title = 'Gas Inlet Temperature'
pressure_tab.add_plot(plot_id='gas_inlet_temperature', title=gas_inlet_temperature_plot_title, channels=['gas_inlet_temperature'],
                       x_axis=('Time since present', 's'), y_axis=('Temperature', 'degC'),
                       offsets=[gas_inlet_temperature_offset], group='Gas Inlet')

filter_line_gas_flow_plot_title = 'Filter Line Gas Flowrate'
pressure_tab.add_plot(plot_id='filter_line_flow', title=filter_line_gas_flow_plot_title, channels=['filter_line_flow'],
                       x_axis=('Time since present', 's'), y_axis=('Flowrate', 'SLPM'),
                       offsets=[filter_line_gas_flow_offset], group='Filter Line')

filter_line_pressure_plot_title = 'Filter Line Pressure'
pressure_tab.add_plot(plot_id='filter_line_pressure', title=filter_line_pressure_plot_title, channels=['filter_line_pressure'],
                       x_axis=('Time since present', 's'), y_axis=('Pressure', 'Torr'),
                       offsets=[filter_line_pressure_offset], group='Filter Line')

filter_line_temperature_plot_title = 'Filter Line Temperature'
pressure_tab.add_plot(plot_id='filter_line_temperature', title=filter_line_temperature_plot_title, channels=['filter_line_temperature'],
                       x_axis=('Time since present', 's'), y_axis=('Temperature', 'degC'),
                       offsets=[filter_line_temperature_offset], group='Filter Line')

filter_line_H2O_plot_title = 'Filter Line H2O Concentration'
pressure_tab.add_plot(plot_id='filter_line_h2o', title=filter_line_H2O_plot_title, channels=['filter_line_h2o'],
                       x_axis=('Time since present', 's'), y_axis=('Concentration', 'ppm'),
                       offsets=[filter_line_H2O_offset], group='Filter Line')

#pressure tab controls
log_interval_options = [('2s', 2), ('10s', 10), ('1m', 60), ('10m', 600), ('1hr', 3600)]
pressure_tab.add_logger_control(id='log_ov_pressure', label='OV Pressure', script='log_pressure.py',
                                 log_filepath=outer_vessel_pressure_log_filepath, port='COM3',
                                 interval_options=log_interval_options, default_interval=2)
pressure_tab.add_logger_control(id='log_h2o', label='H2O Concentration', script='log_H2O_readings.py',
                                 log_filepath=vaisala_H2O_log_filepath, port='COM5',
                                 interval_options=log_interval_options, default_interval=2)

'''VMM TEMPERATURES TAB -- tile grid + one overlay plot (§5.4), not 16 separate plots'''
vmm_plot_ids = [f'vmm_temp_{i}' for i in range(num_vmms)]
temp_tab = plotter.build_vmm_tab('VMM Temperatures', vmm_plot_ids)

'''STATUS STRIP -- always visible above the tabs, regardless of which one is selected'''
plotter.set_status_strip([
    'ov_pressure_g1', 'ov_pressure_g2', 'gauge_pressure',
    'gas_inlet_flow', 'gas_inlet_flow_setpoint', 'filter_line_h2o',
    AggregateTile(label='VMM max', channels=vmm_plot_ids, reduce='max', jump_to_tab='VMM Temperatures'),
])

plotter.run()
