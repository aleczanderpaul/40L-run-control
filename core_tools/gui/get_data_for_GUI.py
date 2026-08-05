import pandas as pd
from datetime import datetime
import numpy as np
import io

'''This module provides functions to read data from a file and process it for GUI display.'''

def read_last_n_rows(data_filepath, n, chunk_size=65536, delimiter=',', has_header=True, column_names=None):
    with open(data_filepath, 'rb') as f:
        # Read and keep the header line separately -- we need it for column
        # names, but it must not be counted as one of the n data rows
        header = f.readline() if has_header else b''

        # Jump to the end of the file to get its total size.
        # (0, 2) means "seek 0 bytes relative to the end of the file"
        f.seek(0, 2)
        file_size = f.tell()

        # Walk backward from the end in fixed-size chunks, collecting bytes
        # until we've seen at least n+1 newlines (the +1 covers a partial
        # line at whatever byte boundary we stop on)
        pos = file_size
        newlines_found = 0
        chunks = []

        while pos > len(header) and newlines_found <= n:
            read_size = min(chunk_size, pos - len(header))
            pos -= read_size
            f.seek(pos)
            chunk = f.read(read_size)
            chunks.append(chunk)
            newlines_found += chunk.count(b'\n')

        # Chunks were collected back-to-front (last chunk of the file first),
        # so reverse them before joining to restore correct byte order
        tail_bytes = b''.join(reversed(chunks))

    # Split into individual lines and keep only the last n
    # (we likely over-read by a partial line or two, this trims it exactly)
    lines = tail_bytes.splitlines()[-n:] if n>0 else []

    # Reassemble header + trimmed data lines into something pandas can parse
    # directly from memory, without ever touching the earlier part of the file
    data_text = header + b'\n'.join(lines) + b'\n'
    if has_header:
        return pd.read_csv(io.BytesIO(data_text), delimiter=delimiter)
    else:
        return pd.read_csv(io.BytesIO(data_text), delimiter=delimiter, header=None, names=column_names)

def read_last_n_rows_filtered(data_filepath, n, vmm_num, chunk_size=65536, delimiter=',', has_header=True, column_names=None):
    with open(data_filepath, 'rb') as f:
        header = f.readline() if has_header else b''

        f.seek(0, 2)
        file_size = f.tell()

        pos = file_size
        chunks = []
        matched = []

        while pos > len(header) and len(matched) < n:
            read_size = min(chunk_size, pos - len(header))
            pos -= read_size
            f.seek(pos)
            chunk = f.read(read_size)
            chunks.append(chunk)

            # re-filter everything read so far each time, working from
            # the current back-position to the end of the file
            tail_bytes = b''.join(reversed(chunks))
            all_lines = tail_bytes.splitlines()

            matched = []
            for line in all_lines:
                parts = line.split(delimiter.encode())
                if len(parts) < 5:
                    continue
                try:
                    fec, hyb, vmm = int(parts[1]), int(parts[2]), int(parts[3])
                except ValueError:
                    continue
                if fec * 8 + hyb * 2 + vmm == vmm_num:
                    matched.append(line)

    lines = matched[-n:] if n > 0 else []

    data_text = header + b'\n'.join(lines) + b'\n'
    if has_header:
        return pd.read_csv(io.BytesIO(data_text), delimiter=delimiter)
    else:
        return pd.read_csv(io.BytesIO(data_text), delimiter=delimiter, header=None, names=column_names)

def get_seconds_ago(dataframe):
    # Convert the 'Time' column in the dataframe from string to datetime objects
    # using the specified format: 'Year-Month-Day Hour:Minute:Second'
    if "Time" in dataframe.columns:
        dataframe['timestamp'] = pd.to_datetime(dataframe['Time'], format='%Y-%m-%d %H:%M:%S')
    elif "timestamp" in dataframe.columns:
        dataframe['timestamp'] = pd.to_datetime(dataframe['timestamp'], format='%Y-%m-%d %H:%M:%S')
    else:
        raise ValueError("DataFrame must contain either a 'Time' or 'timestamp' column")

    # Get the current time as a datetime object
    current_time = datetime.now()

    # Calculate the time difference between current_time and each timestamp in seconds
    # The subtraction produces a timedelta object, and .dt.total_seconds() converts it to float seconds
    # The negative sign (-) in front makes the value represent "seconds ago" as a negative number,
    # meaning past times will be negative
    dataframe['seconds_ago'] = -(current_time - dataframe['timestamp']).dt.total_seconds()

    # Return the new 'seconds_ago' Series from the dataframe
    return dataframe['seconds_ago']

def get_seconds_ago_1904_epoch(dataframe):
    dataframe['timestamp'] = pd.to_datetime(dataframe['Time'], unit='s', origin='1904-01-01')

    current_time = datetime.now()
    dataframe['seconds_ago'] = -(current_time - dataframe['timestamp']).dt.total_seconds()

    return dataframe['seconds_ago']

def get_outer_vessel_gauge_pressure(dataframe, gauge_num):
    # Convert gauge values to numeric, coercing errors (like 'Off') to NaN
    gauge = pd.to_numeric(dataframe[f'Gauge {gauge_num}'], errors='coerce')

    # Identify rows where gauge is on (not NaN)
    g_On_indices = np.where(~gauge.isna())[0]

    # Initialize the pressure array with NaN for all rows, rows not filled later mean no valid pressure reading
    pressure = np.full(len(dataframe), np.nan)

    # Assign pressure values based on the gauge state
    pressure[g_On_indices] = gauge[g_On_indices]

    #Invalidate pressure if units are off
    units = dataframe['Units']

    units_not_valid_indices = np.where(units == 'Off')[0]

    pressure[units_not_valid_indices] = np.nan

    #Convert to Torr
    units_Pascal_indices = np.where(units == 'Pascal')[0]
    pressure[units_Pascal_indices] = pressure[units_Pascal_indices] * 0.0075006168

    units_Bar_indices = np.where(units == 'Bar')[0]
    pressure[units_Bar_indices] = pressure[units_Bar_indices] * 750.06

    # Return the pressure values as a pandas Series with the same index as the input DataFrame
    return pd.Series(pressure, name='Pressure', index=dataframe.index)

def get_inner_vessel_pressure(dataframe):
    flowRate = pd.to_numeric(dataframe['Alicat_Abs_Press_torr'], errors='coerce') #will need to change column name if it changes in FlowVision2
    return pd.Series(flowRate, name='Pressure', index=dataframe.index)

def get_flowrate(dataframe):
    flowRate = pd.to_numeric(dataframe['FlowRate'], errors='coerce')

    #Invalidate flowrate if units are bad
    units = dataframe['FlowRateUnits']

    units_not_valid_indices = np.where(units == 'Bad')[0]

    flowRate[units_not_valid_indices] = np.nan

    #Convert to L/min
    units_SCCM_indices = np.where(units == 'SCCM')[0]
    flowRate[units_SCCM_indices] = flowRate[units_SCCM_indices] / 1000.0

    return pd.Series(flowRate, name='Flowrate', index=dataframe.index)

def get_temperature(dataframe):
    temperature = pd.to_numeric(dataframe['temperature'], errors='coerce')

    # Return the temperature values as a pandas Series with the same index as the input DataFrame
    return pd.Series(temperature, name='Temperature', index=dataframe.index)

def get_n_XY_datapoints(data_filepath, n, datatype, vmm_num):
    if datatype == 'temperature':
        dataframe = read_last_n_rows_filtered(data_filepath, n, vmm_num)
        times = get_seconds_ago(dataframe)
        temperature = get_temperature(dataframe)
        return times, temperature
    elif datatype == 'gauge_pressure':
        dataframe = read_last_n_rows(data_filepath, n, delimiter='\t', has_header=False, column_names=['Time', 'Voltage'])
        times = get_seconds_ago_1904_epoch(dataframe)
        gauge_pressure = dataframe['Voltage']  # In our specific case, the gauge pressure sensor has a 5 Torr range and output is -5V to +5V, so 1V = 1 Torr
        return times, gauge_pressure

    dataframe = read_last_n_rows(data_filepath, n)
    # Depending on the requested datatype, process and return the appropriate data
    if datatype == 'outer_vessel_gauge_1_pressure':
        times = get_seconds_ago(dataframe)
        pressures = get_outer_vessel_gauge_pressure(dataframe, 1)
        return times, pressures
    elif datatype == 'outer_vessel_gauge_2_pressure':
        times = get_seconds_ago(dataframe)
        pressures = get_outer_vessel_gauge_pressure(dataframe, 2)
        return times, pressures
    elif datatype == 'inner_vessel_pressure':
        times = get_seconds_ago(dataframe)
        pressures = get_inner_vessel_pressure(dataframe)
        return times, pressures
    elif datatype == 'flowrate':
        times = get_seconds_ago(dataframe)
        flowrates = get_flowrate(dataframe)
        return times, flowrates
    else:
        # Raise an error if the datatype is not supported
        raise ValueError(f"Unsupported datatype: {datatype}. Supported types are: 'outer_vessel_gauge_1_pressure', 'outer_vessel_gauge_2_pressure', 'inner_vessel_pressure', 'gauge_pressure', 'flowrate', 'temperature'.")