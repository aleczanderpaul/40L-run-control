import io
import os
import time
import datetime

import pandas as pd
import pytest

from core_tools.gui.get_data_for_GUI import read_last_n_rows_filtered, get_seconds_ago


def _write_vmm_file(path, n_timestamps, start_dt):
    '''16-channel VMM-style file (fec 0-1, hyb 0-3, vmm 0-1) with n_timestamps full
    sweeps, using \\r\\n line endings to also exercise that path.'''
    with open(path, 'wb') as f:
        f.write(b'timestamp,fec,hyb,vmm,temperature\r\n')
        for t in range(n_timestamps):
            ts = (start_dt + datetime.timedelta(seconds=2 * t)).strftime('%Y-%m-%d %H:%M:%S')
            for fec in range(2):
                for hyb in range(4):
                    for vmm in range(2):
                        channel = fec * 8 + hyb * 2 + vmm
                        temp = 20.0 + channel + t * 0.001
                        f.write(f"{ts},{fec},{hyb},{vmm},{temp}\r\n".encode())


def test_filtered_read_matches_full_file_filter(tmp_path):
    path = str(tmp_path / 'vmm.csv')
    start = datetime.datetime.now() - datetime.timedelta(seconds=2 * 500)
    _write_vmm_file(path, n_timestamps=500, start_dt=start)

    full = pd.read_csv(path)
    for vmm_num in (0, 5, 7, 15):
        expected = full[full['fec'] * 8 + full['hyb'] * 2 + full['vmm'] == vmm_num].tail(20)
        got = read_last_n_rows_filtered(path, n=20, vmm_num=vmm_num)
        assert list(got['temperature']) == pytest.approx(list(expected['temperature']))
        assert list(got['timestamp']) == list(expected['timestamp'])


def test_filtered_read_respects_n_and_chronological_order(tmp_path):
    path = str(tmp_path / 'vmm.csv')
    start = datetime.datetime.now() - datetime.timedelta(seconds=2 * 2000)
    _write_vmm_file(path, n_timestamps=2000, start_dt=start)

    got = read_last_n_rows_filtered(path, n=50, vmm_num=3, chunk_size=8192)
    assert len(got) == 50
    timestamps = pd.to_datetime(got['timestamp'])
    assert list(timestamps) == sorted(timestamps), "rows must come back in chronological order"


def test_filtered_read_is_not_quadratic(tmp_path):
    # A crude but meaningful guard: doubling the file size should not roughly
    # quadruple the time (the O(n^2) bug would re-filter all accumulated bytes on
    # every chunk, which scales badly as matches get sparser deeper into the file).
    small_path = str(tmp_path / 'vmm_small.csv')
    big_path = str(tmp_path / 'vmm_big.csv')
    start = datetime.datetime.now() - datetime.timedelta(seconds=2 * 8000)
    _write_vmm_file(small_path, n_timestamps=2000, start_dt=start)
    _write_vmm_file(big_path, n_timestamps=8000, start_dt=start)

    t0 = time.perf_counter()
    read_last_n_rows_filtered(small_path, n=50, vmm_num=9, chunk_size=8192)
    small_elapsed = time.perf_counter() - t0

    t0 = time.perf_counter()
    read_last_n_rows_filtered(big_path, n=50, vmm_num=9, chunk_size=8192)
    big_elapsed = time.perf_counter() - t0

    print(f"small(2000 rows/ts)={small_elapsed:.4f}s big(8000 rows/ts, 4x)={big_elapsed:.4f}s")
    # Linear scaling would be ~4x; a generous 10x ceiling still clearly rules out
    # quadratic scaling (which would be ~16x) without being a flaky timing test.
    assert big_elapsed < small_elapsed * 10 + 0.05


def test_get_seconds_ago_correct_in_a_non_utc_timezone():
    original_tz = os.environ.get('TZ')
    try:
        os.environ['TZ'] = 'Pacific/Honolulu'  # no DST, UTC-10 -- a real timezone the doc calls out
        if hasattr(time, 'tzset'):
            time.tzset()

        now_local_naive = datetime.datetime.now()
        df = pd.DataFrame({'Time': [now_local_naive.strftime('%Y-%m-%d %H:%M:%S')]})
        seconds_ago = get_seconds_ago(df)
        assert abs(seconds_ago.iloc[0]) < 5, f"a 'now' timestamp should read as ~0 seconds ago, got {seconds_ago.iloc[0]}"
    finally:
        if original_tz is None:
            os.environ.pop('TZ', None)
        else:
            os.environ['TZ'] = original_tz
        if hasattr(time, 'tzset'):
            time.tzset()


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v']))
