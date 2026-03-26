import time
import csv
import os
from .gas_flow_controller_serial_class import GF100Serial

'''Functions to handle gas flow readings and log them to a CSV file'''

# Reads pressure and unit data from the sensor, converting values as needed
def get_flow_reading(sensor, maxFlow, maxFlowUnits):
    flowPercent = sensor.indicated_flow() #percentage of the max flow rate
    if type(flowPercent) == float or type(flowPercent) == int:
        flowRate = maxFlow * (flowPercent/100.0) #measured flow rate in same units as maxFlow (e.g., L/min, SCCM)
    else:
        flowRate = 'Bad'
    return flowPercent, flowRate, maxFlowUnits

# Creates a new CSV file with a header row if it doesn't already exist
def create_flow_log_csv(filepath):
    if not os.path.exists(filepath):  # Check if the file already exists
        with open(filepath, mode='w', newline='') as file:  # Open in write mode
            writer = csv.writer(file)
            writer.writerow(['Time', 'FlowPercent', 'FlowRate', 'FlowRateUnits'])  # Write column headers

#Logs pressure readings to CSV at regular intervals indefinitely or for a set duration
def log_flow_to_csv(sensor, filepath, interval_sec, maxFlow, maxFlowUnits, duration_sec=None): #None by default means run indefinitely unless specified
    start_time = time.time()

    with open(filepath, mode='a', newline='') as file:  # Open in append mode
        writer = csv.writer(file)

        while duration_sec is None or time.time() - start_time < duration_sec:  # Loop indefinitely or keep looping until time is up
            flowPercent, flowRate, FlowRateUnits = get_flow_reading(sensor, maxFlow, maxFlowUnits)  # Read current values
            timestamp = time.strftime('%Y-%m-%d %H:%M:%S')         # Format current time

            writer.writerow([timestamp, flowPercent, flowRate, FlowRateUnits])  # Write to CSV
            file.flush()               # Flush Python’s internal buffer
            os.fsync(file.fileno())   # Force OS to flush file to disk
            print(f"{timestamp} - Flow Percent: {flowPercent}%, Flow Rate: {flowRate} {FlowRateUnits}")  # Console log, uncomment for debugging
            time.sleep(interval_sec)  # Wait before next reading

    sensor.close_port()  # Close serial connection when done

# Example usage
if __name__ == '__main__':
    log_filepath = '40L_run_control/flow_log.csv'  # CSV log file path

    create_flow_log_csv(log_filepath)  # Ensure the file exists and has a header

    pressureSensor = GF100Serial('COM3', baudrate=115200, macID=36)  # Initialize sensor on COM4
    log_flow_to_csv(sensor=pressureSensor, filepath=log_filepath, interval_sec=2, maxFlow=0.4, maxFlowUnits='L/min')  # Start logging
