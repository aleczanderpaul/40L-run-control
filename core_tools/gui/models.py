from dataclasses import dataclass, field

from core_tools.alarms import AlarmSpec

'''Declarative data model for channels and plots.

A "channel" is a named data source (a file + a column/datatype to pull out of it).
A "plot" is a visual display of one or more channels sharing a set of axes. Channels
are registered once on the LivePlotter (so they can be read for the status strip and
alarm evaluation even without a plot); plots reference channels by id.
'''


@dataclass
class Channel:
    id: str
    label: str
    long_label: str
    filepath: str
    datatype: str
    units: str
    log_interval_s: float
    alarm: AlarmSpec | None = None
    vmm_num: int | None = None
    overview_group: str | None = None  # heading this channel appears under on the Overview tab; None = not shown there


@dataclass
class Plot:
    plot_id: str
    title: str
    channel_ids: list[str]
    x_axis: tuple[str, str]
    y_axis: tuple[str, str]
    offsets: list[float]
    group: str | None = None

    # Runtime state, populated by LiveTab.add_plot() and mutated as the plot runs.
    plot_widget: object = None
    curves: list = field(default_factory=list)
    value_labels: list = field(default_factory=list)   # one per channel, in channel_ids order
    threshold_lines: list = field(default_factory=list)
    note_lines: list = field(default_factory=list)  # reusable operator-note markers, repositioned each scan tick
    pause_button: object = None
    running: bool = False
    # Follow/frozen is independent of running (pause): a frozen plot still receives
    # new data, it just isn't scrolling to show it, whereas a paused plot receives
    # none. Neither affects alarm evaluation. following flips to False the moment the
    # user zooms or pans (ViewBox.sigRangeChangedManually) and back to True from the
    # header indicator, Resume Following (All), or a time-window change.
    following: bool = True
    follow_button: object = None
    in_alarm_visual: bool = False  # tracks current border/title styling so we only touch Qt state on change
    container_widget: object = None  # the grid cell's outer widget, for scroll-into-view on tile/banner click


@dataclass
class AggregateTile:
    label: str
    channels: list[str]
    reduce: str  # 'max' or 'min'
    jump_to_tab: str


@dataclass
class LoggerControl:
    id: str
    label: str
    log_filepath: str
    interval_options: list  # [(option_label, seconds), ...]
    default_interval: float
    # Both None for a bus logger: the bus owns the port and does the talking, so this
    # control has no script to launch and no port of its own.
    script: str | None = None
    port: str | None = None
    # Extra positional arguments this script needs between the port and the interval
    # (e.g. an Alicat's unit id and unit type). Empty for a script that takes the
    # standard <log_filepath> <port> <interval>.
    extra_args: list = field(default_factory=list)
    # When set, this logger has no subprocess of its own: it runs as a polled unit on
    # that shared SerialBus, which owns the port. unit_id/unit_type identify it there.
    bus_id: str | None = None
    unit_id: str | None = None
    unit_type: str | None = None

    # Runtime state, populated by ControlDock.add_logger_group() and mutated as it runs.
    process: object = None
    stderr_lines: list = field(default_factory=list)
    running: bool = False
    user_stopped: bool = True
    pending_interval: float | None = None
    # Consecutive rejected frames for this unit on a shared bus. A single bad frame is
    # a transient the next poll usually clears, so this debounces before the control
    # goes red -- the same reasoning as AlarmSpec.consecutive_samples.
    poll_errors: int = 0
    last_poll_error: str = ''
    # Set when this control has stopped because something went wrong, cleared when it
    # is started or stopped deliberately. The dock scrolls, so a red LED can be off
    # screen -- this is what the pinned fault summary counts.
    faulted: bool = False

    box: object = None  # this control's group box, so the fault summary can scroll to it
    port_combo: object = None
    interval_combo: object = None
    start_stop_button: object = None
    led: object = None
    error_label: object = None


@dataclass
class SetpointControl:
    '''One Alicat MFC setpoint control. Unlike a LoggerControl, which the dock starts
    and stops, this sends one command per Set press -- over a shared bus, or as a
    short one-shot subprocess of its own. Either way there is nothing to stop, so
    there's no start/stop button and no running/crashed lifecycle, only the outcome of
    the last command.'''
    id: str
    label: str
    unit_id: str
    units: str
    min_value: float
    max_value: float
    decimals: int
    default_value: float
    confirm: bool
    # Both None for a bus control, for the same reason as LoggerControl above.
    script: str | None = None
    port: str | None = None

    # Runtime state, populated by ControlDock.add_setpoint_group() and mutated as
    # commands are sent. `sending` guards against a second command being fired while
    # one is still in flight on the same serial port.
    sending: bool = False
    last_sent_value: float | None = None
    # Which id the in-flight command was issued on behalf of ('operator', or an
    # interlock's id). The result handler needs it to tell an operator's command from
    # a safety command that has to be retried if it didn't land.
    command_source: str | None = None
    # Set to a tripped interlock's id while that interlock latches this control. A
    # locked control refuses every operator command until the interlock is reset.
    locked_by: str | None = None
    # When set, commands go to that shared SerialBus instead of spawning a one-shot
    # subprocess -- required whenever something else may be holding the same port.
    bus_id: str | None = None
    # Bumped on every dispatch so a timeout fired for one command can never act on a
    # later one that has since taken its place.
    command_token: int = 0

    box: object = None  # this control's group box, so the fault summary can scroll to it
    port_combo: object = None
    value_spinbox: object = None
    send_button: object = None
    led: object = None
    status_label: object = None


@dataclass
class Interlock:
    """One automatic safety action: when any trigger channel goes into alarm, drive a
    setpoint control to its safe value and latch it there.

    The trigger is the *alarm state machine's* transition, not a raw comparison, so
    the interlock inherits that channel's debounce (AlarmSpec.consecutive_samples)
    and trips on exactly the condition the operator already sees in the banner --
    there is deliberately no second, separately-tunable threshold that could drift
    out of agreement with the alarm limit declared in launch_GUI.py."""
    id: str
    label: str
    trigger_channel_ids: list[str]
    setpoint_control_id: str
    safe_value: float
    trip_on_stale: bool = True

    # Runtime state, populated by ControlDock.add_interlock_group() and mutated as it
    # trips and is reset.
    #
    # An interlock starts DISARMED and arms the first time every trigger channel
    # reads OK. Without that, launching the GUI before the pressure logger is started
    # trips it instantly -- the log file still holds yesterday's rows, so the channel
    # goes STALE within seconds of startup -- which would fire a doomed MFC command
    # and raise a red banner on every single launch. An interlock that cries wolf at
    # startup is one operators learn to ignore, which costs more safety than it buys.
    # Arming is one-way: once a channel has been seen good, losing it later is a real
    # loss of signal and must trip.
    armed: bool = False
    tripped: bool = False
    trip_reason: str = ''
    trip_timestamp: float | None = None
    # Whether the safe-value command was actually acknowledged by the controller.
    # Tripped-but-unconfirmed is the dangerous state: the interlock fired but the gas
    # may still be flowing, so it is retried and shown in red until it lands.
    command_confirmed: bool = False
    attempts: int = 0
    next_attempt_time: float | None = None
    gave_up: bool = False

    box: object = None  # this control's group box, so the fault summary can scroll to it
    led: object = None
    status_label: object = None
    reset_button: object = None


@dataclass
class SerialBus:
    """One serial port shared by several controls, owned by one subprocess.

    Several Alicat units can sit on a single RS-485 adapter, and a serial port has
    exactly one owner -- so a logger process per unit plus a one-shot setpoint process
    cannot coexist: whichever opens first locks the others out. A bus replaces them
    with one process that holds the port and serves every control attached to it.

    `users` is what keeps the port open exactly as long as something needs it: each
    running logger holds a reference for as long as it runs, and each setpoint command
    holds one for as long as it is in flight. The process is started when the set goes
    from empty to non-empty and shut down when it goes back to empty -- so the port
    opens on the first control used and closes when the last one is done with it.
    """
    id: str
    label: str
    script: str
    port: str

    # Runtime state, populated by ControlDock.add_serial_bus_group().
    process: object = None
    users: set = field(default_factory=set)
    ready: bool = False
    # Why the port is currently unusable, or None while it is fine. The bus PROCESS
    # starting successfully says nothing about the PORT -- it is opened lazily on the
    # first poll -- so without this the panel showed green LEDs for a port that could
    # never be opened, with the only evidence buried in the event log.
    port_fault: str | None = None
    stderr_lines: list = field(default_factory=list)
    user_stopped: bool = True

    box: object = None  # this control's group box, so the fault summary can scroll to it
    led: object = None
    port_combo: object = None
    status_label: object = None
