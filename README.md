# 40L-Run-Control

Repository of code for a run control program for the 40L TPC. Intended to measure/plot vessel pressure and VMM temperature in real time, control the experiment remotely, and alarm on out-of-range readings.

## Running it

Install Python and `pip install -r requirements.txt` (add `-r requirements-dev.txt` instead if you also want to run the test suite). Then:

```
python launch_GUI.py
```

## Architecture

- `core_tools/gui/live_plotter_GUI_class.py` is the GUI backend (`LivePlotter`, `LiveTab`, `OverviewTab`, `VMMTab`, `ControlDock`, `StatusStrip`, `AlarmBanner`, `EventLog`).
- `launch_GUI.py` is the *design* — the only file a scientist needs to touch to add a channel, plot, alarm limit, logger, or status-strip tile. It's declarative: register channels, then plots/tabs that reference them by id.
- `core_tools/alarms.py` is the alarm state machine. It's pure Python (no Qt) so it can be unit-tested and, later, run headless.
- Data logging runs in **separate subprocesses** that write plain CSV/DAT files; the GUI only ever reads those files. This is deliberate — logs survive a GUI crash and stay usable for offline analysis. Never move logging into the GUI process.
- A single scan timer on `LivePlotter` (default 1s) reads every registered channel's file on a background thread, evaluates alarms, and pushes fresh data into every unpaused plot. There's no per-plot timer — pausing a plot only stops its own curve redraw; the channel keeps being evaluated for alarms regardless.

## launch_GUI.py API

### Channels

A channel is a data source, declared once, independent of whether any plot displays it (a channel with no plot is still evaluated for alarms and can still appear in the status strip):

```python
plotter.add_channel(
    id='ov_pressure_g1',             # stable key, used everywhere else below
    label='OV g1',                   # short label, for tiles/strip/readouts
    long_label='Outer Vessel Gauge 1 Pressure',
    filepath=outer_vessel_pressure_log_filepath,
    datatype='outer_vessel_gauge_1_pressure',   # dispatch key -- see get_data_for_GUI.py
    units='Torr',
    log_interval_s=2,                # REQUIRED -- drives staleness and the time-window row count
    alarm=AlarmSpec(high=760.0, clear_high=750.0),   # optional
    vmm_num=None,                     # 0-15 for VMM temperature channels only
    overview_group=None,              # heading on the Overview tab, e.g. 'Outer Vessel'
)
```

Supported `datatype` values, and the file format each expects, are documented at the top of `core_tools/gui/get_data_for_GUI.py`.

### Alarms

`AlarmSpec` (`core_tools/alarms.py`) fields: `high`, `low`, `clear_high`, `clear_low`, `abs_high`, `clear_abs_high` (all optional — set the ones relevant to that channel), `consecutive_samples=3` (debounce), `stale_multiplier=5` (staleness = `stale_multiplier * log_interval_s` seconds without a fresh sample). All thresholds are declared here, in `launch_GUI.py`, only — there's no runtime threshold editing and no separate config file.

Alarm evaluation always uses each channel's **raw** value, never an offset-adjusted one, so tuning a plot's display offset can't silently move a trip point.

States are `OK`, `ALARM`, `STALE`, `NO_DATA` — there's no warn tier. Alarms **latch**: when a value returns in range the banner doesn't disappear, it shows "cleared, peak X at HH:MM:SS" until acknowledged, so a transient during an unattended stretch is never silently lost.

### Plots

```python
gas_tab.add_plot(
    plot_id='ov_pressure',           # stable key, never shown to the user
    title='Outer Vessel Pressure',   # display string, freely renameable
    channels=['ov_pressure_g1', 'ov_pressure_g2'],
    x_axis=('Time since present', 's'),
    y_axis=('Pressure', 'Torr'),
    offsets=[0, 0],                  # one per channel, display-only
    group='Outer Vessel',            # which QGroupBox on this tab it's wrapped in
)
```

Multi-channel plots get a real legend (colors are never encoded in the title). Each channel with an `alarm` gets a dashed threshold line (offset-corrected so it still lines up with the trace) and the plot's border/title turn red while any of its channels is in `ALARM`. How much history is shown is governed by the global time-window selector in the control dock, not a per-plot setting.

### Tabs

- `plotter.build_overview_tab()` — call this **before** any other tab so it lands first. Dashboard of tiles (value + 5-minute sparkline) grouped by each channel's `overview_group`; no live plots. Build it after every `add_channel()` call it should reflect.
- `plotter.create_tab(tab_name, plots_per_row)` — a regular tab; call `.add_plot(...)` on the result.
- `plotter.build_vmm_tab(tab_name, channel_ids, threshold=None)` — the VMM Temperatures tab: a 4×4 tile grid (checkbox, current value, alarm color) beside one overlay plot of every checked channel's curve. `threshold=None` derives the single threshold line from the first channel's `AlarmSpec.high`.

### Logger controls

```python
gas_tab.add_logger_control(
    id='log_ov_pressure',
    label='OV Pressure',
    script='log_pressure.py',
    log_filepath=outer_vessel_pressure_log_filepath,
    port='COM3',                     # default; overridable from a live port dropdown at runtime
    interval_options=[('2s', 2), ('10s', 10), ('1m', 60), ('10m', 600), ('1hr', 3600)],
    default_interval=2,
)
```

Builds a group box (LED, port dropdown from `serial.tools.list_ports`, interval dropdown, Start/Stop) in the shared control dock. The subprocess is launched as `[sys.executable, script, log_filepath, port, str(interval)]` — a real argv list, never a shell string, so filenames and ports can contain spaces. An unexpected exit turns the LED red and shows the last stderr line; it never silently flips back to "running". Changing the interval while a logger is running is stashed and applied on the next start.

### Status strip

Always visible above the tabs:

```python
plotter.set_status_strip([
    'ov_pressure_g1', 'ov_pressure_g2', 'gauge_pressure',
    'filter_line_flow', 'filter_line_pressure', 'filter_line_h2o',
    AggregateTile(label='VMM max', channels=[f'vmm_temp_{i}' for i in range(16)],
                  reduce='max', jump_to_tab='VMM Temperatures'),
])
```

A plain channel id becomes a tile showing its label/value/units. `AggregateTile` reduces (`'max'` or `'min'`) over several channels and shows the worst one's value plus its index. Clicking any tile jumps to that channel's detail plot (or, for an `AggregateTile`, to `jump_to_tab`).

### Time window and other global controls

The control dock (right-hand pane) also carries: the global time-window selector (`1m`/`5m`/`15m`/`1h`/`6h`/`24h`, persisted via `QSettings`; each plot fetches `ceil(window_s / channel.log_interval_s)` rows, decimated to ~20000 points via min/max-per-bucket bucketing if that's exceeded so a spike is never hidden), Pause All / Resume All (curve redraws only — never affects alarm evaluation), and Acknowledge All.

### Event log

Bottom pane, collapsible, filterable. Every alarm transition, logger start/stop/crash, captured subprocess stderr, and runtime threshold/interval change is logged there and mirrored to a timestamped `event_log_*.log` file in the working directory, so it survives a GUI crash the same way the data logs do.

## Testing

```
pytest tests/ -v
```

`core_tools/alarms.py` and `core_tools/gui/decimate.py` are pure Python with no Qt dependency, so their test suites run headless with no display. GUI code can also be smoke-tested headlessly with `QT_QPA_PLATFORM=offscreen python launch_GUI.py`.
