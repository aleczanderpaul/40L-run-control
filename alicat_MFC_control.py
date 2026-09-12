from core_tools.AlicatMFC.Alicat_MFC_serial_class import AlicatMFCSerial
import sys

#To run script, use format: python3  <serial_port> <unit_id> <setpoint>
#If using venv, use format: .venv\Scripts\python.exe <serial_port> <unit_id> <setpoint>

serial_port = sys.argv[1]
unit_id = sys.argv[2]
setpoint = float(sys.argv[3])

AlicatMFC = AlicatMFCSerial(serial_port, unit_id)
setpointCommand = AlicatMFC.set_flow_setpoint(setpoint)
print(setpointCommand)
AlicatMFC.close_port()