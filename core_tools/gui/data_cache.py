import os
import pandas as pd
from pyqtgraph.Qt import QtCore

from .get_data_for_GUI import (
    read_last_n_rows, get_seconds_ago, get_seconds_ago_1904_epoch,
    get_outer_vessel_gauge_pressure, get_filter_line_flowrate, get_filter_line_pressure,
    get_filter_line_temperature, get_filter_line_H2O_concentration, get_VMM_temperature,
)

'''One disk read per file per scan tick, fanned out to every channel that needs it --
this replaces the old design where 16 VMM plots each independently re-scanned
vmm_temperatures.csv every tick. A DataFileCache instance is scoped to a single scan
(constructed fresh each tick, discarded after), so it never needs eviction logic:
each (filepath, mtime, ...) key it sees within that scan is read from disk at most
once, and the whole cache is simply garbage collected once the tick's results have
been reported back.'''


class DataFileCache:
    def __init__(self):
        self._cache = {}

    def get(self, filepath, n, delimiter=',', has_header=True, column_names=None):
        mtime = os.path.getmtime(filepath)  # raises OSError/FileNotFoundError -- caller decides how to handle it
        key = (filepath, mtime, delimiter, has_header, tuple(column_names) if column_names else None)
        if key not in self._cache:
            self._cache[key] = read_last_n_rows(filepath, n, delimiter=delimiter, has_header=has_header, column_names=column_names)
        return self._cache[key]


def get_n_xy_cached(cache, filepath, n, datatype, vmm_num, read_n=None):
    '''Same (times, values) contract as get_n_XY_datapoints in get_data_for_GUI.py,
    but sources its raw DataFrame from `cache` instead of reading the file directly.
    `read_n` is how many rows to actually fetch from disk (the largest any caller
    sharing this file needs this tick); `n` is how many this particular caller wants
    sliced off the end of that shared read. VMM channels share one file across 16
    different vmm_num filters, so each does its own in-memory filter + .tail(n)
    against the one shared read rather than a second per-channel disk scan.'''
    read_n = read_n if read_n is not None else n

    if datatype == 'vmm_temperature':
        raw = cache.get(filepath, read_n)
        channel = pd.to_numeric(raw['fec'], errors='coerce') * 8 + pd.to_numeric(raw['hyb'], errors='coerce') * 2 + pd.to_numeric(raw['vmm'], errors='coerce')
        filtered = raw[channel == vmm_num].tail(n).copy()
        return get_seconds_ago(filtered), get_VMM_temperature(filtered)

    if datatype == 'gauge_pressure':
        raw = cache.get(filepath, read_n, delimiter='\t', has_header=False, column_names=['Time', 'Voltage'])
        df = raw.tail(n).copy()
        return get_seconds_ago_1904_epoch(df), df['Voltage']

    raw = cache.get(filepath, read_n)
    df = raw.tail(n).copy()
    if datatype == 'outer_vessel_gauge_1_pressure':
        return get_seconds_ago(df), get_outer_vessel_gauge_pressure(df, 1)
    elif datatype == 'outer_vessel_gauge_2_pressure':
        return get_seconds_ago(df), get_outer_vessel_gauge_pressure(df, 2)
    elif datatype == 'filter_line_flowrate':
        return get_seconds_ago(df), get_filter_line_flowrate(df)
    elif datatype == 'filter_line_pressure':
        return get_seconds_ago(df), get_filter_line_pressure(df)
    elif datatype == 'filter_line_temperature':
        return get_seconds_ago(df), get_filter_line_temperature(df)
    elif datatype == 'filter_line_H2O_concentration':
        return get_seconds_ago(df), get_filter_line_H2O_concentration(df)
    else:
        raise ValueError(f"Unsupported datatype: {datatype}")


# A vmm_temperature request for n matching rows needs roughly 16x as many RAW rows
# read from the shared file (16 channels interleaved, ~evenly), plus margin -- this
# is a heuristic, not a guarantee; an unlucky distribution could still under-fill a
# channel's window by a few points, which is fine (fewer displayed points, not wrong
# data) and is why get_n_xy_cached's own .tail(n) still trims correctly either way.
VMM_READ_SAFETY_FACTOR = 20


class ScanWorkerSignals(QtCore.QObject):
    finished = QtCore.pyqtSignal(dict)  # channel_id -> ('ok', x, y) | ('error', message)


class ScanRunnable(QtCore.QRunnable):
    '''Runs entirely on a QThreadPool worker thread: no Qt widgets, no
    self.plotter -- pure file I/O and pandas, so it's safe off the GUI thread.
    Results are handed back to the GUI thread via a signal, never a direct call.'''

    def __init__(self, requests):
        # requests: [(channel_id, filepath, n, datatype, vmm_num), ...]
        super().__init__()
        self.requests = requests
        self.signals = ScanWorkerSignals()

    def run(self):
        cache = DataFileCache()

        file_max_n = {}
        for _channel_id, filepath, n, datatype, _vmm_num in self.requests:
            effective_n = n * VMM_READ_SAFETY_FACTOR if datatype == 'vmm_temperature' else n
            file_max_n[filepath] = max(file_max_n.get(filepath, 0), effective_n)

        results = {}
        for channel_id, filepath, n, datatype, vmm_num in self.requests:
            try:
                x, y = get_n_xy_cached(cache, filepath, n, datatype, vmm_num, read_n=file_max_n[filepath])
            except (pd.errors.ParserError, ValueError, OSError) as e:
                results[channel_id] = ('error', str(e))
                continue
            results[channel_id] = ('ok', x, y)

        self.signals.finished.emit(results)
