from core_tools.AlicatTools.alicat_bus_server_functions import run
import sys

#To run script, use format: python3 <alicat_bus_server.py filepath> <serial_port>
#If using venv, use format: .venv\Scripts\python.exe <alicat_bus_server.py filepath> <serial_port>
#
#Owns one Alicat RS-485 bus and serves every unit on it. Takes JSON commands on stdin
#(poll/stop/set/quit) and reports JSON events on stdout -- see
#core_tools/AlicatTools/alicat_bus_server_functions.py for the protocol. Launched and
#managed by the GUI's control dock, not normally run by hand.

serial_port = sys.argv[1]

run(serial_port)
