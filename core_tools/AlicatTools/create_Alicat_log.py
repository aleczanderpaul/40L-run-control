import csv
import os

def create_Alicat_log_csv(filepath, unitType):
    if unitType == "MFC":
        headers = ['Time', 'abs_pressure_Torr', 'temperature_C', "volumetric_flow_LPM", "mass_flow_SLPM", "mass_flow_setpoint_SLPM", "gas_type"]
    elif unitType == "Sensor Only":
        headers = ['Time', 'abs_pressure_PSI', 'temperature_C', "volumetric_flow_LPM", "mass_flow_SLPM", "gas_type"]
    else:
        raise ValueError(f"Unsupported unit type: {unitType}. Valid unit types are: MFC, Sensor Only.")
    if not os.path.exists(filepath):  # Check if the file already exists
        with open(filepath, mode='w', newline='') as file:  # Open in write mode
            writer = csv.writer(file)
            writer.writerow(headers)  # Write column headers