import time
import csv
import os
from .pressure_sensor_serial_class import MKSPDR2000Serial

'''Functions to handle pressure readings and log them to a CSV file'''

# Converts string to float unless the value is 'Off', in which case it leaves it as 'Off'
def convert_str_to_float(value):
    return float(value) if value != 'Off' else 'Off'

# Reads pressure and unit data from the sensor, converting values as needed
def get_pressure_readings(sensor):
    gauge1, gauge2 = sensor.read_pressure()
    units = sensor.read_units()
    return convert_str_to_float(gauge1), convert_str_to_float(gauge2), units

# Creates a new CSV file with a header row if it doesn't already exist
def create_pressure_log_csv(filepath):
    if not os.path.exists(filepath):  # Check if the file already exists
        with open(filepath, mode='w', newline='') as file:  # Open in write mode
            writer = csv.writer(file)
            writer.writerow(['Time', 'Gauge 1', 'Gauge 2', 'Units'])  # Write column headers

#Logs pressure readings to CSV at regular intervals indefinitely or for a set duration
def log_pressure_to_csv(sensor, filepath, interval_sec, duration_sec=None): #None by default means run indefinitely unless specified
    start_time = time.time()

    with open(filepath, mode='a', newline='') as file:  # Open in append mode
        writer = csv.writer(file)

        while duration_sec is None or time.time() - start_time < duration_sec:  # Loop indefinitely or keep looping until time is up
            gauge1, gauge2, units = get_pressure_readings(sensor)  # Read current values
            timestamp = time.strftime('%Y-%m-%d %H:%M:%S')         # Format current time

            writer.writerow([timestamp, gauge1, gauge2, units])  # Write to CSV
            file.flush()               # Flush Python’s internal buffer
            os.fsync(file.fileno())   # Force OS to flush file to disk
            print(f"{timestamp} - Gauge1: {gauge1}, Gauge2: {gauge2}, Units: {units}")  # Console log, uncomment for debugging
            time.sleep(interval_sec)  # Wait before next reading

    sensor.close_port()  # Close serial connection when done

# Example usage
if __name__ == '__main__':
    log_filepath = '40L_run_control/pressure_log.csv'  # CSV log file path

    create_pressure_log_csv(log_filepath)  # Ensure the file exists and has a header

    pressureSensor = MKSPDR2000Serial('COM4')  # Initialize sensor on COM4
    log_pressure_to_csv(pressureSensor, log_filepath, interval_sec=2)  # Start logging
