import serial
import time

'''Class to handle serial communication with an H2O sensor (specifically the Vaisala DMT143)'''

class VaisalaDMT143Serial:
    def __init__(self, port_name):
        # Initialize the serial connection with specified parameters:
        # port_name: the serial port to connect to (e.g., 'COM4' on Windows)
        # baudrate: 19200 bits per second (communication speed)
        # bytesize: 8 bits per byte
        # parity: no parity bit
        # stopbits: 1 stop bit
        # timeout: 2.1 second timeout for read operations
        # flow control: off
        self.ser = serial.Serial(
            port=port_name,
            baudrate=19200,      
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=2.1,
            xonxoff=False,   # software (XON/XOFF) flow control - off
            rtscts=False,    # hardware RTS/CTS flow control - off
            dsrdtr=False     # hardware DSR/DTR flow control - off
        )  # These settings are specific to the Vaisala DMT143 H2O sensor

        time.sleep(2.1)  # Wait 2.1 seconds for the serial port and device to initialize (from Vaisala documentation)

        self.EscapeRunMode #just in case startup mode is RUN
    
    def _send_command(self, cmd):
        self.ser.reset_input_buffer()
        self.ser.write((cmd + '\r').encode('ascii'))
        time.sleep(0.1)
        # Commands may be echoed back, and multi-field responses may span
        # more than one line - read a bit generously
        raw = self.ser.read(750).decode('utf-8', errors='replace')
        return raw.strip()

    def GetHelp(self):
        return self._send_command('HELP')

    def SetStartupMode(self, mode): #mode = 'RUN' or 'STOP' or 'POLL' or 'MODBUS'
        return self._send_command('SMODE' + ' ' + mode)

    def GetDeviceInfoOutsidePollMode(self):
        return self._send_command('?')

    def GetDeviceInfoInsidePollMode(self):
            return self._send_command('??')

    def SetUnit(self, mORn): #mORn = 'm' for metric or 'n' for non-metric
        return self._send_command('UNIT' + ' ' + mORn)

    def GetReading(self): #will not work if sensor is in POLL mode because an address will be required
        return self._send_command('SEND')

    def EscapeRunMode(self):
        return self._send_command('\x1b') #Send ESC command to escape RUN mode