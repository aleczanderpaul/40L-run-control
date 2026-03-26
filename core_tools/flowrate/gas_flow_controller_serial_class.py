import serial
import time

class GF100Serial:
    def __init__(self, port_name, baudrate=115200, macID=1):
        self.ser = serial.Serial(
            port=port_name,
            baudrate=baudrate,      
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=5
        )
        self.macID = int(macID)

    def new_setpoint(self, flowPercent):
        self.ser.reset_input_buffer()

        newSetpoint = (((0xC000-0x4000)*flowPercent) // 100) + 0x4000
        LSB = newSetpoint & 0xFF
        MSB = (newSetpoint >> 8) & 0xFF

        packet = [self.macID, 0x02, 0x81, 0x05, 0x69, 0x01, 0xA4, LSB, MSB, 0x00]
        checksum = sum(packet[1:]) & 0xFF
        packet.append(checksum)
        
        self.ser.write(bytes(packet))

        time.sleep(0.1)
        response = self.ser.read(2)

        if response[0] == 0x06 & response[1] == 0x06:
            print('Request recieved, write executed')
        elif response[0] == 0x06 & response[1] == 0x16:
            print('Request recieved, write error')
        elif response[0] == 0x16 & response[1] == 0x06:
            print('Request not received, write success?? ERROR')
        elif response[0] == 0x16 & response[1] == 0x16:
            print('Request not received, write error')
        else:
            print('Something horribly wrong ERROR')
    
    def indicated_flow(self):
        self.ser.reset_input_buffer()

        packet = [self.macID, 0x02, 0x80, 0x03, 0x6A, 0x01, 0xA9, 0x00, 0x99]

        self.ser.write(bytes(packet))

        time.sleep(0.1)
        response = self.ser.read(12)

        if response[0] == 0x06:
            print('Packet received (ack)')
            if sum(response[2:10]) & 0xFF == response[11]:
                LSB = response[8]
                MSB = response[9]
                flow = LSB + (MSB << 8)
                flowPercent = (flow - 0x4000)*100 / (0xC000-0x4000)
                print(f'Checksum good, Flow={flowPercent}%')
                return flowPercent
            else:
                print('Checksum bad')
                return 'Bad'
        elif response[0] == 0x16:
            print('nak')
            return 'Bad'
        else:
            print('Something went wrong')
            return 'Bad'
        
    def close_port(self):
        # Close the serial port connection cleanly
        self.ser.close()
    
if __name__ == "__main__":
    test = GF100Serial('COM3', baudrate=115200, macID=36)
    test.new_setpoint(0)
    while True:
        test.indicated_flow()
        time.sleep(10)