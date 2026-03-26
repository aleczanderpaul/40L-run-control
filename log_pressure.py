from core_tools.MKSPDR2000_pressure.save_pressure_readings_functions import create_pressure_log_csv, log_pressure_to_csv
from core_tools.MKSPDR2000_pressure.pressure_sensor_serial_class import MKSPDR2000Serial
import sys

#To run script, use format: python3 <log_pressure.py filepath> <log_filepath (make sure to add .csv)> <serial_port> <interval_sec> <duration_sec (optional, leave empty for indefinite)>
#If using venv, use format: .venv\Scripts\python.exe <log_pressure.py filepath> <log_filepath (make sure to add .csv)> <serial_port> <interval_sec> <duration_sec (optional, leave empty for indefinite)>

log_filepath = sys.argv[1]
serial_port = sys.argv[2]
interval_sec = float(sys.argv[3])
duration_sec = float(sys.argv[4]) if len(sys.argv) > 4 else None

create_pressure_log_csv(log_filepath)  # Ensure the file exists and has a header
pressureSensor = MKSPDR2000Serial(serial_port)
log_pressure_to_csv(pressureSensor, log_filepath, interval_sec=interval_sec, duration_sec=duration_sec)