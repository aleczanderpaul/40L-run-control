import numpy as np

'''Min/max-per-pixel-bucket decimation. A naive stride (take every Nth point) can
silently hide a spike that falls between the kept samples -- unacceptable for
alarm-relevant data -- so each bucket instead keeps its min AND max value point, in
time order, which preserves every excursion's extremes at the cost of showing 2
points per bucket instead of 1.'''


def decimate_min_max(x, y, max_points=20000):
    x = np.asarray(x)
    y = np.asarray(y)
    n = len(x)
    if n <= max_points:
        return x, y

    bucket_count = max(1, max_points // 2)
    edges = np.linspace(0, n, bucket_count + 1).astype(int)

    out_x = []
    out_y = []
    for i in range(bucket_count):
        start, end = edges[i], edges[i + 1]
        if start >= end:
            continue
        bucket_x = x[start:end]
        bucket_y = y[start:end]

        if np.all(np.isnan(bucket_y)):
            min_idx = max_idx = 0
        else:
            min_idx = np.nanargmin(bucket_y)
            max_idx = np.nanargmax(bucket_y)

        first_idx, second_idx = (min_idx, max_idx) if min_idx <= max_idx else (max_idx, min_idx)
        out_x.append(bucket_x[first_idx])
        out_y.append(bucket_y[first_idx])
        if second_idx != first_idx:
            out_x.append(bucket_x[second_idx])
            out_y.append(bucket_y[second_idx])

    return np.array(out_x), np.array(out_y)
