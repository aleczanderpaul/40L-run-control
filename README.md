# 40L-Run-Control

Repository of code for a run control program for the 40L TPC. Intended to measure/plot vessel pressure and VMM temperature in real time, and to control the experiment remotely.

## launch_GUI.py

Run this script to launch the run control GUI. The widgets (plots, buttons, etc.) to display can be specified in the file using functions in the LivePlotter and LiveTab classes (from core_tools/gui/live_plotter_GUI_class.py). Some notes about how to use launch_GUI.py are left as comments inside the file.

Before running, make sure to install Python and pip install the libraries in requirements.txt