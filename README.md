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
- Instruments sharing one serial adapter are served by a single bus subprocess that owns the port (`core_tools/AlicatTools/alicat_bus_server_functions.py`), opened on first use and closed on last. See "Shared serial buses" below.
- The automatic gas fill is the only thing that commands gas from a measurement rather than a keystroke; see "Automatic gas fill" below for every way it refuses to.
- Safety interlocks (over-pressure shuts the gas inlet MFC) ride the alarm state machine's transitions rather than re-testing values, so a limit is declared exactly once. See "Safety interlocks" below.
- `core_tools/notes.py` is the operator-notes store. Pure Python (no Qt), same reasoning as `alarms.py`.
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
    log_interval_s=2,                # REQUIRED -- drives staleness and the time-window row count.
                                     # A DEFAULT: overridden at runtime by the logger's rate (below).
    alarm=AlarmSpec(high=760.0, clear_high=750.0),   # optional
    vmm_num=None,                     # 0-15 for VMM temperature channels only
    overview_group=None,              # heading on the Overview tab, e.g. 'Outer Vessel'
)
```

Supported `datatype` values are the ones dispatched on in `get_n_XY_datapoints()` in `core_tools/gui/get_data_for_GUI.py` (and mirrored in `get_n_xy_cached()` in `core_tools/gui/data_cache.py` — a new datatype must be added to **both**). The columns each one expects are visible in the `get_*` helper it calls; a datatype must only ever be paired with a `filepath` whose file actually has those columns.

### Alarms

`AlarmSpec` (`core_tools/alarms.py`) fields: `high`, `low`, `clear_high`, `clear_low`, `abs_high`, `clear_abs_high` (all optional — set the ones relevant to that channel), `consecutive_samples=3` (debounce), `stale_multiplier=5` (staleness = `stale_multiplier * log_interval_s` seconds without a fresh sample — and that interval **follows the logger's dropdown at runtime**, see below). All thresholds are declared here, in `launch_GUI.py`, only — there's no runtime threshold editing and no separate config file.

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

Every curve draws a **dot at each sample** joined by the trend line, so the actual sample positions are visible and not just the shape joining them — which is also what makes an irregular or dropped sample obvious instead of being smoothed into the line.

The count that matters is the **finite** points, not the array length: a window where most samples are NaN draws far fewer dots than it has rows, and a channel that is all NaN draws none at all. That last case is not hypothetical — an intentionally-off gauge (the low-range OV gauge above ~1 Torr) is NaN for every row in the window, and leaving an empty scatter item on it makes pyqtgraph compute bounds with `np.nanmin` over an all-NaN array, which emits `RuntimeWarning: All-NaN slice encountered` once per curve per scan tick and floods the console the logger prints share. The marker decision is therefore applied *before* `setData`, not after — flipping the dots off once the all-NaN data has already landed is a tick too late.

Dots are shown only while they are far enough apart to read, judged against the plot's real width (`POINT_MARKER_MIN_SPACING_PX`, 3px) rather than a fixed point count: the same 450 points are legible on a maximised plot and a solid smear on a narrow one, and the operator resizes the dock, window and splitters constantly. In the default layout a plot's drawing area is ~580px, so the 1m and 5m windows keep their dots and 15m and longer do not. Past `SYMBOL_MAX_POINTS` they are suppressed regardless — beyond `DECIMATION_CAP` the drawn points are min/max per bucket rather than real samples, so dotting them would be a lie as well as a repaint cost (~0.4ms/curve for a line vs ~10.5ms with 20000 dots, on every plot every scan tick). The VMM overlay follows the same rule; the Overview tab's sparklines deliberately don't, being 40px tall.

Multi-channel plots get a real legend (colors are never encoded in the title). Each channel with an `alarm` gets a dashed threshold line (offset-corrected so it still lines up with the trace) and the plot's border/title turn red while any of its channels is in `ALARM`. How much history is shown is governed by the global time-window selector in the control dock, not a per-plot setting.

Each plot's header row carries one current-value readout per channel, a **follow/frozen indicator**, and a pause toggle.

**Follow/frozen.** Zooming or panning a pyqtgraph plot turns its ViewBox's autorange off, after which new data keeps arriving but the visible range stops tracking it — so a plot can look live while showing a frozen window into the past. The indicator answers one question, "am I looking at live data?": `FOLLOWING` (quiet) or `FROZEN` (amber). Clicking it makes the plot live again — unpausing it if it was paused, and re-enabling autorange so the view snaps back to the live time window. **Resume Following (All)** in the control dock re-follows every plot at once (it doesn't unpause — that's Resume All), and changing the time-window selector re-follows too.

A **paused** plot also reads as `FROZEN`, because it isn't showing live data either. The two reasons stay tellable apart: the pause toggle still shows ⏸/▶, and the indicator's tooltip names the actual reason (or both). What differs underneath is that a frozen plot still receives new data and just isn't scrolling to show it, while a paused plot receives none — and neither affects alarm evaluation, which never stops.

Whether a plot is following is derived from the ViewBox's own autorange flags on every `sigStateChanged`, not remembered from a single signal. Autorange is turned off by a zoom or pan and back **on** by three things: the indicator, pyqtgraph's auto-scale button (the small "A" that appears at a plot's bottom left once it's zoomed), and the right-click menu's X/Y "Auto" checkboxes. Watching `sigRangeChangedManually` alone caught the freeze but neither of the latter two ways out of it, which left a stale `FROZEN` badge on a plot that had resumed tracking. Deriving the answer can't drift out of sync that way, and a normal redraw leaves those flags on, so a scan tick never reads as a freeze.

The VMM overlay has its own indicator in its controls row, beside Select All / Select None, and behaves identically.

### Tabs

- `plotter.build_overview_tab()` — call this **before** any other tab so it lands first. Dashboard of tiles (value + 5-minute sparkline) grouped by each channel's `overview_group`; no live plots. Build it after every `add_channel()` call it should reflect.
- `plotter.create_tab(tab_name, plots_per_row)` — a regular tab; call `.add_plot(...)` on the result.
- `plotter.build_vmm_tab(tab_name, channel_ids, threshold=None)` — the VMM Temperatures tab: one overlay plot of every checked channel's curve, with a compact 4-column tile row (checkbox, color swatch, current value, alarm color) underneath so the plot keeps the dominant share of the space. `threshold=None` derives the single threshold line from the first channel's `AlarmSpec.high`. Curve colors come from `core_tools/gui/palette.py`, which generates a visually-distinct color per channel *index* — deterministic, so an operator can learn them, and distinct well past the 32 VMMs the system will eventually have. The tile swatches are the overlay's legend (32 in-plot legend entries would bury the data), so the overlay has none of its own. `COLOR_CYCLE` still colors `LiveTab.add_plot()`'s two- and three-curve plots and is unrelated.
- The Event Terminal tab is added automatically (by `plotter.run()`, after every tab above) — nothing to declare for it.

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

Builds a group box (LED, port dropdown from `serial.tools.list_ports`, interval dropdown, Start/Stop) in the shared control dock. Pass `bus=` instead of relying on `port=` when something else may hold the same port — see "Shared serial buses" below; a bus logger has no port dropdown and no subprocess of its own. `extra_args` supplies any positional arguments a standalone script needs between the port and the interval. The port dropdown always offers the declared port even when it isn't currently enumerated, so a device that's unplugged or off at launch doesn't become unselectable once it's back. The subprocess is launched as `[sys.executable, script, log_filepath, port, str(interval)]` — a real argv list, never a shell string, so filenames and ports can contain spaces. An unexpected exit turns the LED red and shows the last stderr line; it never silently flips back to "running". Changing the interval while a logger is running is stashed and applied on the next start.

**Starting a logger writes its rate onto every channel that reads its log file.** A channel's `log_interval_s` drives when the alarm evaluator calls it `STALE` (`stale_multiplier * log_interval_s`) and how many rows a plot fetches for the time window — both claims about how often data *actually* arrives, which is whatever the operator picked from the dropdown, not what `launch_GUI.py` declared. While the two were independent, switching a logger to `1m` left its channels believing `2s`: every reading arrived ~50 s after the channel had already been declared stale, and a "5m" window drew 150 rows of a file producing 5 in five minutes. The same reconciliation runs once at declaration, so a channel declared `log_interval_s=2` alongside a logger declaring `default_interval=10` can't sit quietly disagreeing before the first start.

The logger control is the authority here because it is what writes the file. A logger and a channel never name each other in `launch_GUI.py` — the log file they share is the only link, which is what `channels_fed_by()` walks (unit-tested in `tests/test_log_interval.py`). Channels written by something outside this program — `gauge_pressure`, the VMM temperatures — have no logger control and keep their declared rate.

### Shared serial buses

Several instruments can sit on one RS-485 adapter, told apart only by unit id — and a serial port has exactly one owner. A logger subprocess per unit plus a one-shot setpoint subprocess therefore cannot coexist on that port: whichever opens first locks the others out with `Access is denied`. A **bus** replaces them with one process that holds the port and serves every control attached to it.

```python
plotter.add_serial_bus(id='alicat_bus', label='Alicat Bus',
                       script='alicat_bus_server.py', port='COM4')
```

Then attach controls to it instead of giving them their own port:

```python
gas_tab.add_logger_control(id='log_gas_inlet_alicat', label='Gas Inlet Alicat',
                            bus='alicat_bus', unit_id='A', unit_type='MFC', ...)
gas_tab.add_setpoint_control(id='setpoint_gas_inlet_mfc', bus='alicat_bus', unit_id='A', ...)
```

Declare the bus **before** them; each control validates the reference and raises on a bad one. The dock lays buses and logger controls out in declaration order, so declaring a bus immediately above the controls that attach to it puts its box next to the state it explains — grouping by widget type instead leaves unrelated instruments sitting between a bus and the controls it serves.

A control talks over a bus **or** over its own subprocess, never both and never neither: `bus=` and `script=`/`port=` are mutually exclusive, and omitting all of them raises. Accepting a bus alongside a script used to silently ignore the script, which left arguments in `launch_GUI.py` that read as load-bearing and weren't — the sort of thing someone later "fixes" by editing a file nothing runs. A bus logger also needs `unit_id` and `unit_type`, since that's how the bus knows which instrument to poll.

**The port opens on first use and closes on last.** The bus keeps a set of users: a running logger holds a reference for as long as it runs, and a setpoint command holds one for as long as it's in flight. The process starts when that set goes from empty to non-empty and is shut down when it goes back to empty. So pressing Set with no logger running opens the port, sends, and closes it again; pressing Set while a logger runs just rides the connection that logger already holds. Nothing has to be started or stopped by hand, and nothing is left holding a port it doesn't need.

The port dropdown lives on the bus, not on the controls — there is one physical port, and three copies of the dropdown could only ever disagree. It's disabled while anything holds the port open, since changing it under a running bus would silently leave every attached control talking to the old one.

**The bus process** (`alicat_bus_server.py`, logic in `core_tools/AlicatTools/alicat_bus_server_functions.py`) polls each registered unit on its own interval, appends to that unit's CSV, and executes setpoints between polls. Logging still happens in a subprocess writing plain CSV — this changes how many processes share a port, not where logging lives. The protocol is JSON, one object per line on stdin/stdout, because unit types and filepaths contain spaces and a space-delimited protocol would need quoting rules nobody would get right. The serial port is touched only from the server's main loop; stdin is read on a worker thread through a queue, because `select()` doesn't work on pipes on Windows.

Two guards the per-unit scripts couldn't have: a polled frame is written only if field 0 — the echoed unit id — matches the unit that was asked, and only if the field count matches that unit type. On a shared bus, a reply from the wrong unit or a stale frame left by an earlier timeout would otherwise file one instrument's readings under another's name. A rejected frame is skipped, logged, and retried on the next poll rather than written.

**A bus process starting successfully says nothing about the port.** The port is opened lazily on the first poll, so a bus whose port is unavailable used to start cleanly, report `ready`, and leave every control showing a green LED while the only evidence was `poll_error` lines in the Event Terminal. The server therefore reports port-level failures (`port_error` / `port_ok`) separately from frame-level ones.

A fault **ends** the affected controls exactly the way an unexpected subprocess exit ends a standalone logger: red LED, the reason inline on the group box, and the button back to `Start`. A control that is red but still offering `Stop` is the half-state that made a broken bus look like a working one — a control either is running or it is not, and the button has to say which. Stopping also releases the bus, so a failed start leaves nothing behind: the port closes and the operator presses Start again once they've fixed whatever held it. The bus box keeps showing `CANNOT OPEN COM4 — …` after that, since a placid "Closed" next to two red controls would hide the reason they went red.

Frame-level errors are debounced: a mis-addressed or short frame is usually transient and the next poll clears it, so a unit's control faults only after `BUS_POLL_ERRORS_BEFORE_FAULT` (3) consecutive bad frames — the same reasoning as `AlarmSpec.consecutive_samples` — and a good frame resets the count. A unit answering with the wrong id faults that unit's control while the bus stays healthy, which is accurate: the port is fine, that instrument isn't.

**When the bus dies,** every control on it is told at once: each running logger goes red with the exit code and last stderr line, and an in-flight setpoint is failed immediately rather than waiting — so a tripped interlock can retry instead of hanging on a reply that will never come. A bus setpoint also carries its own `SETPOINT_TIMEOUT_S` deadline (a one-shot subprocess gets that from `subprocess.run`; a bus command has nothing equivalent), with a token so a timeout fired for one command can never act on a later one.

A bus setpoint's reply is funnelled into the same result path a one-shot subprocess takes, so the dock, the event log and the interlock can't end up treating the two transports differently.

### MFC setpoint controls

```python
gas_tab.add_setpoint_control(
    id='setpoint_gas_inlet_mfc',
    label='Gas Inlet MFC',
    bus='alicat_bus',                # or script=/port= for a unit alone on its own adapter
    unit_id='A',                     # RS-485 address the command is sent to
    units='SLPM',
    min_value=0.0,
    max_value=50.0,                  # the controller's configured full scale
    decimals=2,
    default_value=0.0,
    confirm=True,                    # confirmation dialog before each command
)
```

Builds a group box in the same control dock as the logger controls (its own block below them): LED, a bounded value box, a Set button, and a one-line result. It is **not** a logger and deliberately doesn't look like one — a setpoint is one command, not a process, so there's no Start/Stop, no interval, and no LED lifecycle to watch.

On a bus, each press sends one `set` command to the process that owns the port. Standalone (`script=`/`port=`), each press instead runs that script once as `[sys.executable, script, port, unit_id, str(value)]` — note the argument order differs from a logger's `script log_filepath port interval`.

Either way the command is off the GUI thread and reports back through a signal: opening a serial port, waiting for the device, writing and reading would freeze every plot for seconds if done inline. While it's in flight the LED is amber and the button is disabled, so a second command can't be fired at the same port. `SETPOINT_TIMEOUT_S` (15 s) bounds a controller that never answers.

**A zero exit code does not mean the setpoint took.** The control script prints `ERROR during set setpoint, output: ...` and still exits 0 when the controller's reply isn't a valid data frame — wrong unit id, setpoint source configured for analog instead of Serial/Front Panel, or nothing on the other end of the line. A command counts as acknowledged only when the process exited cleanly *and* the script says the controller acknowledged (`setpoint_acknowledged()`, unit-tested in `tests/test_setpoint_result.py`); anything else gets a red LED, the reply or stderr line shown in the box, and an `ERROR` line in the Event Terminal. Every attempt is logged either way, with the value, units, port and unit id.

The value box is a hard-bounded spin box rather than a free-text field — the controller accepts whatever it's sent, so `min_value`/`max_value` declared here are the only thing between a typo and 500 SLPM. `confirm=True` (the default) additionally names the value, units, port and unit id in a dialog before the command goes out; pass `confirm=False` for a control where that's more friction than it's worth.

Nothing about this control reads the setpoint back — that's the logger's job. The resulting setpoint shows up on the `gas_inlet_flow_setpoint` channel like any other reading, which is also how an operator confirms the controller is where they put it.

In-flight one-shot setpoint commands are **not** killed on GUI shutdown (running loggers are). They're already bounded by the timeout, and killing one mid-write could leave the controller at a value nobody asked for.

### Automatic gas fill

A two-stage fill: open the MFC wide, then ease off before the target so the vessel doesn't sail past it.

```python
plotter.add_fill_control(
    id='ov_gas_fill', label='OV Gas Fill',
    setpoint_control='setpoint_gas_inlet_mfc',
    pressure_channels=['ov_pressure_g1', 'ov_pressure_g2'],
    pressure_units='Torr', max_pressure=760.0, pressure_decimals=1,
    default_fast_flow=10.0,    # rate while there is room
    default_slow_at=700.0,     # hand over to the slow rate here
    default_slow_flow=1.0,     # rate through the last stretch
    default_target=740.0,      # stop here
)
```

All four numbers are the operator's, set from spin boxes at runtime; the declaration only supplies the starting values. Flow bounds are inherited from the setpoint control so the two can't disagree about what the MFC accepts. The boxes lock while a fill runs — changing the numbers a fill is running to would be ambiguous — and unlock when it ends.

Why two stages: the MFC can't stop instantly and the vessel keeps rising after the valve shuts, so a single rate either creeps (slow everywhere) or overshoots (fast to the end).

**This is the only part of the program that opens gas from a measurement rather than an operator's keystroke**, so most of its behaviour is about refusing to. Engaging is refused outright if there is no usable pressure reading, if the slow-down pressure isn't below the target, if the slow rate exceeds the fast rate or is zero, if the pressure is already at the target, or if the target is at or above the over-pressure alarm on the channels it steers by — that last one would trip the interlock at the top of every fill. `confirm=True` (the default) also names all four numbers and the current pressure in a dialog first.

Once running it aborts to zero flow if **no trigger channel is readable** (filling blind is the thing it must never do), if the **interlock latches** the setpoint control, if the **shared bus dies**, or if the MFC rejects `INTERLOCK_MAX_ATTEMPTS` commands in a row. An abort is a fault: red LED, the reason on the box, and a line in the pinned fault summary.

**Flow only ever ratchets down.** `fill_flow_for()` never returns more than the fill has already settled to, so pressure dithering across the handover can't swing the MFC between rates — and a fill whose pressure is *falling* (a leak, or someone pumping) is not a reason to open the valve wider.

It steers by the **highest** reading among its pressure channels that is currently `OK`. Highest because two gauges disagreeing during a fill is a reason to believe the one saying you're closer to the target; `OK`-only because a switched-off low-range gauge is normal and must not veto a fill, while a stale or alarming one contributes nothing. It ticks once per scan, immediately after the interlocks see the same data, so a trip is found before one more command goes out.

The decision logic (`fill_stage_for`, `fill_flow_for`, `fill_settings_error`) is pure and unit-tested in `tests/test_fill.py`.

### Safety interlocks

An interlock is an automatic action: when a channel goes into alarm, drive a setpoint control to a safe value and latch it there.

```python
plotter.add_interlock(
    id='ov_overpressure_stops_gas',
    label='OV Over-pressure',
    trigger_channels=['ov_pressure_g1', 'ov_pressure_g2'],   # any one of them tripping is enough
    setpoint_control='setpoint_gas_inlet_mfc',
    safe_value=0.0,
    trip_on_stale=True,
)
```

Declared on the plotter rather than on a tab — it's a system-wide rule, not part of any one tab's display — and **after** the channels and setpoint control it names. Every reference is validated at declaration time and raises: a typo'd channel id would otherwise produce an interlock that looks armed in the GUI and does nothing when the pressure actually rises, which is the worst failure this feature has. A trigger channel with no `AlarmSpec` is rejected for the same reason — it could never enter `ALARM`.

**It rides the alarm state machine, not a threshold of its own.** The trip condition is the `AlarmTransition` into `ALARM`, so the limit lives in exactly one place (the channel's `AlarmSpec`) and there's no second number that can drift out of agreement with the one on the plot. It also inherits that alarm's debounce, so with `consecutive_samples=3` on a 2 s channel it fires ~6 s after the first breach rather than on a single noisy sample.

`trip_on_stale=True` also trips when the trigger channel stops reporting. A channel that went quiet isn't reading high, but it isn't reading safe either, and gas flowing into a vessel whose pressure nobody is watching is the case this exists to prevent. `NO_DATA` deliberately does **not** trip: the evaluator reaches it on NaN readings, which it treats as a normal intentionally-off gauge.

**Arming.** An interlock starts *disarmed* and arms the first time the quantity it watches is readable — at least one trigger channel reading `OK`, with data actually having arrived. Without this, launching the GUI before the pressure logger is started trips it instantly (the log file still holds the previous run's rows, so the channel goes `STALE` within seconds), firing a doomed MFC command and raising a red banner on every launch. An interlock that cries wolf at startup is one operators learn to ignore, which costs more safety than it buys. Arming is one-way: once a channel has been seen good, losing it later is a real loss of signal and trips. While disarmed the dock says so plainly and locks nothing.

**Latching.** A trip locks the setpoint control: its Set button is disabled (with a tooltip saying why) and the dispatcher refuses commands independently of the widget's enabled flag. **Reset Interlock** unlocks it, and is itself allowed only once the pressure is readable again — the same predicate as arming. That's stricter than "the alarm cleared": `STALE` blocks a reset too, because you can't reopen gas on readings that have stopped arriving.

A channel in `NO_DATA` does **not** block, as long as another trigger channel is reading. `NO_DATA` is how `alarms.py` represents a deliberately-off gauge (`Off` in the log → NaN), and the 40L's low-range OV gauge is switched off above ~1 Torr. Counting it as a blocker meant the interlock could never arm during normal operation and would not have tripped at 765 Torr — the safety feature silently inert exactly when it was needed. It also contradicted the trip rule, which already ignores `NO_DATA` for the same reason: an intentionally-off gauge isn't a fault, it's just not the gauge in use right now. What must never happen is arming with no usable gauge at all, so when *nothing* is reading `OK`, every non-OK channel blocks, `NO_DATA` included. Reset does **not** restore the previous setpoint; flow only ever resumes because someone typed a value and pressed Set.

**If the safe-value command fails,** it's retried every `INTERLOCK_RETRY_DELAY_S` (2 s) up to `INTERLOCK_MAX_ATTEMPTS` (5) — a safety action that quietly failed is worse than none. Bounded, because against a dead port every attempt fails instantly and an unbounded retry would bury the event log in the one situation where the operator most needs to read it. After the cap the interlock stops commanding and says, in red and in the log, `COULD NOT SET ... SHUT THE GAS MANUALLY`. Until the controller acknowledges, the box stays red and reads *tripped but unconfirmed* — the dangerous state, where the interlock fired and the gas may still be flowing. A trip that lands while an operator's command is still on the port isn't a failure and doesn't spend an attempt; it retries as soon as the port frees up.

Every trip, retry, confirmation, refusal and reset is logged to the Event Terminal at `ALARM`/`ERROR` level. The interlock has no presence in the alarm banner of its own — the channel alarm that caused it is already there — so its state lives in its dock group box.

The decision logic (`interlock_should_trip`, `interlock_reset_blockers`) is pure and unit-tested in `tests/test_interlock.py`.

### Status strip

Always visible above the tabs:

```python
plotter.set_status_strip([
    'ov_pressure_g1', 'ov_pressure_g2', 'gauge_pressure',
    'gas_inlet_flow', 'gas_inlet_flow_setpoint', 'filter_line_h2o',
    AggregateTile(label='VMM max', channels=[f'vmm_temp_{i}' for i in range(16)],
                  reduce='max', jump_to_tab='VMM Temperatures'),
])
```

A plain channel id becomes a tile showing its label/value/units. `AggregateTile` reduces (`'max'` or `'min'`) over several channels and shows the worst one's value plus its index. Clicking any tile jumps to that channel's detail plot (or, for an `AggregateTile`, to `jump_to_tab`).

### Control dock layout

The dock's instrument boxes scroll; everything below them is pinned.

Left to itself the dock wanted ~1256px of height with a ~1208px **minimum**, and a Qt layout minimum overrides the maximized window state — so on anything short of a 1440p display it forced the whole window taller than the screen. Only the instrument boxes (buses, loggers, setpoint controls, interlocks) went into the scroll area; the window selector, Pause/Resume, Resume Following and the note input are used constantly and must never scroll away, and at ~194px they are small enough to stay pinned. The dock's minimum height is now 251px, and the window fits 1366x768.

Two columns was the alternative and doesn't work at this width: the dock is only ~320–384px wide at the default splitter ratio, so a fault line like `CANNOT OPEN COM4 — …` wraps to eight or more lines and a *faulted* box ends up taller than a healthy one — content whose height changes with its state, which is the same trap the status strip and alarm banner comments warn about. Making two columns honest would mean roughly doubling the dock's width, which comes out of the plots.

The scroll area keeps its horizontal scrollbar **off**, so the content's minimum *width* still propagates out and the splitter can't collapse the dock narrower than a Start button, while the height — the dimension that was overflowing — is free to scroll.

Each section inside the scroll area is a real widget rather than a bare nested `QVBoxLayout`: a layout added to another layout doesn't reliably push a size change up the chain when a widget is added to it later, so the scroll content reported a height of 0, the scroll area concluded everything fit, and the content was silently clipped with no scrollbar.

**The fault summary** is pinned above the scroll area and hidden whenever nothing is wrong. Once the boxes scroll, a red LED can be off screen — which would undo the point of making failures obvious — so this does for the dock what the alarm banner does for channels: it names every faulted bus, stopped-by-error logger and tripped interlock, and clicking it scrolls the first one into view. It's recomputed from state on the same 500ms tick that polls loggers, rather than maintained incrementally: a dozen call sites setting a flag is a dozen chances to leave the summary claiming all-clear over a red LED.

### Time window and other global controls

The control dock (right-hand pane) also carries: the global time-window selector (`1m`/`5m`/`15m`/`1h`/`6h`/`24h`, persisted via `QSettings`; each plot draws `ceil(window_s / channel.log_interval_s)` rows, decimated to ~20000 points via min/max-per-bucket bucketing if that's exceeded so a spike is never hidden. The scan *reads* at least `ALARM_LOOKBACK_ROWS` rows regardless, so alarm evaluation keeps enough history at short windows; the extra rows are trimmed before drawing, which is why the two counts are separate functions — `rows_to_fetch()` and `rows_for_window()`), Pause All / Resume All (curve redraws only — never affects alarm evaluation), Resume Following (All), and the operator-note input. Acknowledging alarms is done from the alarm banner (Acknowledge / Acknowledge All).

### Operator notes

A one-line note input, always visible in the control dock (not in the Event Terminal tab — the operator has to be able to jot a note without leaving whatever tab they're watching). Type a note, press Enter, and three things happen:

1. It's logged at a `NOTE` level, so it appears in the Event Terminal and in that day's on-disk event log alongside everything else.
2. It's appended to **`operator_notes.csv`** in the working directory (columns `timestamp,note`), flushed on every write. The timestamp is `yyyy-MM-dd HH:mm:ss` in naive local time — deliberately the same format every sensor log here uses, so a note lines up with the data it annotates and one analysis script can parse both. This is a first-class data product for offline analysis, same reasoning as the data logs: it survives a GUI crash and any analysis script can read it.
3. A vertical dotted blue marker is drawn at that timestamp on every plot, including the VMM overlay — styled unlike the dashed red threshold lines so a note is never mistaken for a limit. Hovering a marker shows its text.

Notes are stored as absolute timestamps, but plots use a "seconds since present" X axis, so each marker's X is recomputed as `note_time - now` on every scan tick and drifts left along with the data. Markers outside the current time window are hidden rather than piling up at the left edge. On startup the last 24 h of `operator_notes.csv` is reloaded, so markers survive a restart.

### Event Terminal

A tab of its own, added after every tab `launch_GUI.py` declares, filterable. Every alarm transition, logger start/stop/crash, captured subprocess stderr, and runtime threshold/interval change is logged there and mirrored to disk in `event_logs/`, one file per calendar date (`event_log_YYYY-MM-DD.log`) rather than one per launch, so relaunching the program the same day keeps appending to that day's file and it survives a GUI crash the same way the data logs do.

## Testing

```
pytest tests/ -v
```

`core_tools/alarms.py`, `core_tools/gui/decimate.py`, `core_tools/gui/palette.py` and `core_tools/notes.py` are pure Python with no Qt dependency, so their test suites run headless with no display. GUI code can also be smoke-tested headlessly with `QT_QPA_PLATFORM=offscreen python launch_GUI.py`.
