'''How an MFC setpoint command's outcome is read off its subprocess.

Importing live_plotter_GUI_class pulls in PyQt5, but only as an import -- these two
functions are pure string handling and no QApplication (and so no display) is
created, so this suite runs headless. Same arrangement as test_row_counts.py.

The regression these guard: the Alicat control script reports a *rejected* setpoint by
printing "ERROR during set setpoint" and exiting 0. Judging the command by its exit
code would show the operator a green LED for a flow the controller never took.
'''

from core_tools.gui.live_plotter_GUI_class import last_line, setpoint_acknowledged


class TestLastLine:
    def test_single_line(self):
        assert last_line('Successfully set setpoint, output: [...]') == 'Successfully set setpoint, output: [...]'

    def test_takes_the_last_of_several(self):
        assert last_line('warming up\nSuccessfully set setpoint, output: [...]') == 'Successfully set setpoint, output: [...]'

    # A subprocess's captured output almost always ends in a newline, so the last
    # split element is blank -- picking it blindly would report "no output".
    def test_ignores_trailing_blank_lines(self):
        assert last_line('Successfully set setpoint\n\n  \n') == 'Successfully set setpoint'

    def test_strips_surrounding_whitespace(self):
        assert last_line('  padded reply  \n') == 'padded reply'

    def test_no_output(self):
        assert last_line('') == ''
        assert last_line('   \n\n') == ''
        assert last_line(None) == ''


class TestSetpointAcknowledged:
    def test_clean_exit_and_success_reply(self):
        assert setpoint_acknowledged(0, "Successfully set setpoint, output: ['A', '+015.44', ...]")

    # The success message has been reworded once already. Matching more of it than the
    # leading word turned every successful command into a reported failure, which for
    # the interlock means crying wolf on a safe-value command that actually landed.
    def test_reworded_success_message_still_counts(self):
        assert setpoint_acknowledged(0, "Successfully set Alicat MFC setpoint, output: ['A']")

    # The whole point: the script exits 0 on a rejected setpoint.
    def test_clean_exit_but_error_reply(self):
        assert not setpoint_acknowledged(0, 'ERROR during set setpoint, output: []')

    # No reply at all (device off, wrong port) is a failure, not a success by default.
    def test_clean_exit_but_no_reply(self):
        assert not setpoint_acknowledged(0, '')

    def test_nonzero_exit(self):
        assert not setpoint_acknowledged(1, 'Successfully set setpoint, output: [...]')

    # 'ERROR during set Alicat MFC setpoint' must stay a failure however it is worded.
    def test_reworded_error_message_is_still_a_failure(self):
        assert not setpoint_acknowledged(0, 'ERROR during set Alicat MFC setpoint, output: []')

    # The timeout/OSError paths in _run_setpoint emit -1 with the reason on stderr,
    # so stdout is empty and the command must not read as applied.
    def test_timeout_sentinel(self):
        assert not setpoint_acknowledged(-1, '')
