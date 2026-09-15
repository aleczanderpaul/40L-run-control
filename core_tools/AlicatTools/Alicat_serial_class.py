import serial
import time

'''Class to handle serial communication Alicat Mass Flow Controller'''

class AlicatSerial:
    def __init__(self, port_name, unit_id, unit_type):
        # Initialize the serial connection with specified parameters:
        # port_name: the serial port to connect to (e.g., 'COM4' on Windows)
        # baudrate: 19200 bits per second (communication speed)
        # bytesize: 8 bits per byte
        # parity: no parity bit
        # stopbits: 1 stop bit
        # timeout: 1 second timeout for read operations
        self.ser = serial.Serial(
            port=port_name,
            baudrate=19200,      
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=1
        )

        self.unit_id = unit_id
        self.unit_type = unit_type

        time.sleep(1)  # Wait 1 second for the serial port and device to initialize

    def get_unit_type(self):
        return self.unit_type

    def get_measurements(self):
        self.ser.reset_input_buffer()

        command = f"{self.unit_id}\r"
        self.ser.write(command.encode())
        
        response = self.ser.read_until(expected=b'\r').decode(errors="replace").strip()
        parts = response.split()
        return parts

    def set_flow_setpoint(self, setpoint): #only for Alicat MFC, NOT the sensor only one
        self.ser.reset_input_buffer()
        
        """
        Set a new flow setpoint on the controller.

        Per the Alicat MPL manual, the command format is:
            <unit_id>S <setpoint_value>\r
        """
        command = f"{self.unit_id}S {setpoint}\r"
        self.ser.write(command.encode())

        response = self.ser.read_until(expected=b'\r').decode(errors="replace").strip()

        # Expected reply is a data frame like: "A +015.44 ..." — the second
        parts = response.split()
        if len(parts) == 7:
            try:
                return f'Successfully set Alicat MFC setpoint, output: {parts}'
            except ValueError:
                pass
        return f'ERROR during set Alicat MFC setpoint, output: {parts}'

    def close_port(self):
        # Close the serial port connection cleanly
        self.ser.close()

# Example usage
if __name__ == "__main__":
    AlicatMFC = AlicatSerial("COM4", "A", "MFC")

    print(AlicatMFC.get_measurements())

    print(AlicatMFC.get_unit_type())

    print(AlicatMFC.set_flow_setpoint(50))

    print(AlicatMFC.set_flow_setpoint(0))

    AlicatMFC.close_port()