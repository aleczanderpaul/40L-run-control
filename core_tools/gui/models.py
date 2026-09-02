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
    pause_button: object = None
    interval_timer: object = None
    elapsed_timer: object = None
    running: bool = False
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
    script: str
    log_filepath: str
    interval_options: list  # [(option_label, seconds), ...]
    default_interval: float
    port: str

    # Runtime state, populated by ControlDock.add_logger_group() and mutated as it runs.
    process: object = None
    stderr_lines: list = field(default_factory=list)
    running: bool = False
    user_stopped: bool = True
    pending_interval: float | None = None

    port_combo: object = None
    interval_combo: object = None
    start_stop_button: object = None
    led: object = None
    error_label: object = None
