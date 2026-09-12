import serial
import time

'''Class to handle serial communication Alicat Mass Flow Controller'''

class AlicatMFCSerial:
    def __init__(self, port_name, unit_id):
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

        time.sleep(1)  # Wait 1 second for the serial port and device to initialize

    def set_flow_setpoint(self, setpoint):
        """
        Set a new flow setpoint on the controller.

        Per the Alicat MPL manual, the command format is:
            <unit_id>S <setpoint_value>\r

        The instrument's setpoint source must be configured for
        Serial/Front Panel (not analog) for this to take effect, and the
        controller interprets the value in whatever engineering units it's
        currently configured to use.
        """
        command = f"{self.unit_id}S {setpoint}\r"
        self.ser.write(command.encode())

        response = self.ser.read_until(expected=b'\r').decode(errors="replace").strip()

        # Expected reply is a data frame like: "A +015.44 ..." — the second
        parts = response.split()
        if len(parts) == 7:
            try:
                return f'Successfully set setpoint, output: {parts}'
            except ValueError:
                pass
        return f'ERROR during set setpoint, output: {parts}'

    def close_port(self):
        # Close the serial port connection cleanly
        self.ser.close()

# Example usage
if __name__ == "__main__":
    AlicatMFC = AlicatMFCSerial("COM4", "A")

    print(AlicatMFC.set_flow_setpoint(50))

    print(AlicatMFC.set_flow_setpoint(0))

    AlicatMFC.close_port()