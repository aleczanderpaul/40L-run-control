import time
import csv
import os
from .Alicat_serial_class import AlicatSerial

def get_Alicat_readings(sensor=None):
    raw = sensor.get_measurements()
    unitType = sensor.get_unit_type()
    return [unitType] + raw

def create_Alicat_log_csv(filepath, unitType):
    if unitType == "MFC":
        headers = ['Time', 'abs_pressure_Torr', 'temperature_C', "volumetric_flow_LPM", "mass_flow_SLPM", "mass_flow_setpoint_SLPM", "gas_type"]
    elif unitType == "Sensor Only":
        headers = ['Time', 'abs_pressure_Torr', 'temperature_C', "volumetric_flow_LPM", "mass_flow_SLPM"]
    else:
        raise(NameError("Valid unit types are: MFC, Sensor Only"))
    if not os.path.exists(filepath):  # Check if the file already exists
        with open(filepath, mode='w', newline='') as file:  # Open in write mode
            writer = csv.writer(file)
            writer.writerow(headers)  # Write column headers

def log_Alicat_to_csv(sensor, filepath, interval_sec, duration_sec=None): #None by default means run indefinitely unless specified
    start_time = time.time()

    with open(filepath, mode='a', newline='') as file:  # Open in append mode
        writer = csv.writer(file)

        while duration_sec is None or time.time() - start_time < duration_sec:  # Loop indefinitely or keep looping until time is up
            readings = get_Alicat_readings(sensor)
            unitType = readings[0]
            timestamp = time.strftime('%Y-%m-%d %H:%M:%S')         # Format current time
            rowOfData = [timestamp] + readings[2:]

            writer.writerow(rowOfData)  # Write to CSV
            file.flush()               # Flush Python’s internal buffer
            os.fsync(file.fileno())   # Force OS to flush file to disk
            print(f'Alicat Sensor {unitType}: {rowOfData}')  # Console log, uncomment for debugging
            time.sleep(interval_sec)  # Wait before next reading

    sensor.close_port()  # Close serial connection when done

# Example usage
if __name__ == '__main__':
    log_filepath = 'alicat_test_log.csv'  # CSV log file path

    create_Alicat_log_csv(log_filepath, 'MFC')  # Ensure the file exists and has a header

    AlicatSensor = AlicatSerial('COM4', 'A', "MFC")  # Initialize sensor on COM4
    log_Alicat_to_csv(AlicatSensor, log_filepath, interval_sec=2)  # Start logging