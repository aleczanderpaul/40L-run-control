from core_tools.AlicatTools.save_Alicat_readings_functions import create_Alicat_log_csv, log_Alicat_to_csv
from core_tools.AlicatTools.Alicat_serial_class import AlicatSerial
import sys

#To run script, use format: python3  <log_Alicat_readings.py filepath> <log_filepath (make sure to add .csv)> <serial_port> <unit_id> <unit_type> <interval_sec> <duration_sec (optional, leave empty for indefinite)>
#If using venv, use format: .venv\Scripts\python.exe <log_Alicat_readings.py filepath> <log_filepath (make sure to add .csv)> <serial_port> <unit_id> <unit_type> <interval_sec> <duration_sec (optional, leave empty for indefinite)>

log_filepath = sys.argv[1]
serial_port = sys.argv[2]
unit_id = sys.argv[3]
unit_type = sys.argv[4]
interval_sec = float(sys.argv[5])
duration_sec = float(sys.argv[6]) if len(sys.argv) > 6 else None

create_Alicat_log_csv(log_filepath, unit_type)  # Ensure the file exists and has a header
AlicatEquipment = AlicatSerial(serial_port, unit_id, unit_type)
log_Alicat_to_csv(AlicatEquipment, log_filepath, interval_sec=interval_sec, duration_sec=duration_sec)