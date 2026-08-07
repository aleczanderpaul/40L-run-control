from core_tools.VaisalaDMT143_H2Osensor.save_H2O_sensor_readings_functions import create_H2O_log_csv, log_H2O_to_csv
from core_tools.VaisalaDMT143_H2Osensor.H2O_sensor_serial_class import VaisalaDMT143Serial
import sys

#To run script, use format: python3 <log_H2O_readings.py filepath> <log_filepath (make sure to add .csv)> <serial_port> <interval_sec> <duration_sec (optional, leave empty for indefinite)>
#If using venv, use format: .venv\Scripts\python.exe <log_H2O_readings.py filepath> <log_filepath (make sure to add .csv)> <serial_port> <interval_sec> <duration_sec (optional, leave empty for indefinite)>

log_filepath = sys.argv[1]
serial_port = sys.argv[2]
interval_sec = float(sys.argv[3])
duration_sec = float(sys.argv[4]) if len(sys.argv) > 4 else None

create_H2O_log_csv(log_filepath)  # Ensure the file exists and has a header
H2OSensor = VaisalaDMT143Serial(serial_port)
log_H2O_to_csv(H2OSensor, log_filepath, interval_sec=interval_sec, duration_sec=duration_sec)