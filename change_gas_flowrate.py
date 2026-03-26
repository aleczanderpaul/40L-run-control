from core_tools.flowrate.gas_flow_controller_serial_class import GF100Serial
import sys

#To run script, use format: python3 <log_pressure.py filepath> <log_filepath (make sure to add .csv)> <serial_port> <interval_sec> <duration_sec (optional, leave empty for indefinite)>
#If using venv, use format: .venv\Scripts\python.exe <log_pressure.py filepath> <log_filepath (make sure to add .csv)> <serial_port> <interval_sec> <duration_sec (optional, leave empty for indefinite)>

serial_port = sys.argv[1]
flowPercent = int(sys.argv[2])

flowController = GF100Serial(serial_port, baudrate=115200, macID=36)
flowController.new_setpoint(flowPercent)
flowController.close_port()
#if baudrate, macID, change for the mass flow controller, you will have to manually change it here