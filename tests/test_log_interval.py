'''Which channels a logger's rate applies to.

Importing live_plotter_GUI_class pulls in PyQt5, but only as an import -- this
function is a plain filter and no QApplication (and so no display) is created, so
this suite runs headless. Same arrangement as test_row_counts.py.

The regression this guards: a channel's log_interval_s drives both when the alarm
evaluator calls it STALE (stale_multiplier x interval) and how many rows a plot
fetches for the time window. Both are claims about how often data ACTUALLY arrives.
The value declared in launch_GUI.py is only a default -- the operator sets the real
rate from the logger's interval dropdown at runtime. While the two were independent,
switching the dropdown to 1m left the channel believing 2s, so every reading arrived
~50s after the channel had already been declared STALE, and a "5m" plot window drew
150 rows of a file that only produces 5 in five minutes.

A logger control and a channel never name each other in launch_GUI.py; the log file
they share is the only link, which is what channels_fed_by walks.
'''

from dataclasses import dataclass

from core_tools.gui.live_plotter_GUI_class import channels_fed_by


@dataclass
class FakeChannel:
    filepath: str


OV = 'outer_vessel_pressure_log.csv'
H2O = 'H2O_log.csv'

CHANNELS = {
    'ov_pressure_g1': FakeChannel(OV),
    'ov_pressure_g2': FakeChannel(OV),
    'filter_line_h2o': FakeChannel(H2O),
    'gauge_pressure': FakeChannel('gauge_pressure_log_0.dat'),
}


# Two channels read the outer-vessel log, and one logger writes it -- so changing its
# rate has to move both, not whichever happened to be found first.
def test_every_channel_reading_the_file_is_returned():
    assert sorted(channels_fed_by(CHANNELS, OV)) == ['ov_pressure_g1', 'ov_pressure_g2']


def test_channels_on_other_files_are_untouched():
    assert channels_fed_by(CHANNELS, H2O) == ['filter_line_h2o']


# gauge_pressure and the VMM temperatures are written by processes outside this
# program, so no logger control owns them and nothing may rewrite their declared rate.
def test_a_file_no_logger_writes_matches_nothing():
    assert channels_fed_by(CHANNELS, 'vmm_temperatures.csv') == []


def test_no_channels_at_all():
    assert channels_fed_by({}, OV) == []
