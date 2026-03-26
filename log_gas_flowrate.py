from core_tools.flowrate.save_gas_flow_readings_functions import create_flow_log_csv, log_flow_to_csv
from core_tools.flowrate.gas_flow_controller_serial_class import GF100Serial
import sys

#To run script, use format: python3 <log_pressure.py filepath> <log_filepath (make sure to add .csv)> <serial_port> <interval_sec> <duration_sec (optional, leave empty for indefinite)>
#If using venv, use format: .venv\Scripts\python.exe <log_pressure.py filepath> <log_filepath (make sure to add .csv)> <serial_port> <interval_sec> <duration_sec (optional, leave empty for indefinite)>

log_filepath = sys.argv[1]
serial_port = sys.argv[2]
interval_sec = float(sys.argv[3])
duration_sec = float(sys.argv[4]) if len(sys.argv) > 4 else None

create_flow_log_csv(log_filepath)  # Ensure the file exists and has a header
flowController = GF100Serial(serial_port, baudrate=115200, macID=36)
log_flow_to_csv(sensor=flowController, filepath=log_filepath, interval_sec=interval_sec, maxFlow=0.4, maxFlowUnits='L/min', duration_sec=duration_sec)
#if baudrate, macID, maxFlow (maximum flowrate), and/or maxFlowUnits (units of maxFlow) change for the mass flow controller, you will have to manually change it here