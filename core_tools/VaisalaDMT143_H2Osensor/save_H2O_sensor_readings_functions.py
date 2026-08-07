import time
import csv
import os
import re
from .H2O_sensor_serial_class import VaisalaDMT143Serial

def get_H2O_readings(sensor=None):
    raw = sensor.GetReading()

    pattern = r'(\w+)=\s*(-?\d+\.?\d*)'
    matches = re.findall(pattern, raw)

    readings = {'Tdf': 'None', 'Tdfatm': 'None', 'H2O': 'None'}  # guaranteed keys, default 'None'
    readings.update({label: float(value) for label, value in matches if label in readings})

    return readings

def create_H2O_log_csv(filepath):
    if not os.path.exists(filepath):  # Check if the file already exists
        with open(filepath, mode='w', newline='') as file:  # Open in write mode
            writer = csv.writer(file)
            writer.writerow(['Time', 'Tdf_F', 'Tdfatm_F', 'H2O_ppm'])  # Write column headers

def log_H2O_to_csv(sensor, filepath, interval_sec, duration_sec=None): #None by default means run indefinitely unless specified
    start_time = time.time()

    with open(filepath, mode='a', newline='') as file:  # Open in append mode
        writer = csv.writer(file)

        while duration_sec is None or time.time() - start_time < duration_sec:  # Loop indefinitely or keep looping until time is up
            readings = get_H2O_readings(sensor)
            Tdf_F = readings.get('Tdf')
            Tdfatm_F = readings.get('Tdfatm')
            H2O_ppm = readings.get('H2O')

            timestamp = time.strftime('%Y-%m-%d %H:%M:%S')         # Format current time

            writer.writerow([timestamp, Tdf_F, Tdfatm_F, H2O_ppm])  # Write to CSV
            file.flush()               # Flush Python’s internal buffer
            os.fsync(file.fileno())   # Force OS to flush file to disk
            print(f"{timestamp} - Tdf_F: {Tdf_F}, Tdfatm_F: {Tdfatm_F}, H2O_ppm: {H2O_ppm}")  # Console log, uncomment for debugging
            time.sleep(interval_sec)  # Wait before next reading

    sensor.close_port()  # Close serial connection when done


# Example usage
if __name__ == '__main__':
    log_filepath = 'H2O_log.csv'  # CSV log file path

    create_H2O_log_csv(log_filepath)  # Ensure the file exists and has a header

    H2OSensor = VaisalaDMT143Serial('COM7')  # Initialize sensor on COM7
    log_H2O_to_csv(H2OSensor, log_filepath, interval_sec=2)  # Start logging