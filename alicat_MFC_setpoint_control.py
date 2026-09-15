from core_tools.AlicatTools.Alicat_serial_class import AlicatSerial
import sys

#To run script, use format: python3 <alicat_MFC_setpoint_control.py filepath> <serial_port> <unit_id> <setpoint>
#If using venv, use format: .venv\Scripts\python.exe <alicat_MFC_setpoint_control.py filepath> <serial_port> <unit_id> <setpoint>

serial_port = sys.argv[1]
unit_id = sys.argv[2]
setpoint = float(sys.argv[3])

AlicatMFC = AlicatSerial(serial_port, unit_id, "MFC")
setpointCommand = AlicatMFC.set_flow_setpoint(setpoint)
print(setpointCommand)
AlicatMFC.close_port()