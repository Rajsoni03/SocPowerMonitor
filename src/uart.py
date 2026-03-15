import traceback
import pexpect
import serial
from pexpect import fdpexpect

LOG_NONE = -1
LOG_ERROR = 0
LOG_WARNING = 1
LOG_INFO = 2

_log_level_dict = {
    LOG_NONE: 'NONE',
    LOG_ERROR: 'ERROR',
    LOG_WARNING: 'WARNING',
    LOG_INFO: 'INFO'
}

class UartSetupIssue(Exception):
    """Exception raised for errors in the UART setup process."""
    def __init__(self, message):
        self.message = message
        super().__init__(self.message)

class Uart:
    def __init__(self, uart_port, baudrate=115200, log_file_path=None, log_level=LOG_INFO):
        self.uart_port_info = {
            "port": uart_port,
            "baudrate": baudrate,
            "bytesize": 8,
            "parity": "N",
            "stopbits": 1,
            "timeout": None,
            "xonxoff": 0,
            "rtscts": 0
        }
        self.log_file_path = log_file_path
        self.serial_conn_obj = None
        self.file_descriptor_process = None
        self.log_level = log_level
        self.log_file_obj = None
    
    def __del__(self):
        self.disconnect()
        if self.log_level <= LOG_INFO:
            print('[ Info ] Deleting UartInterface object and releasing the resources.')

    def set_log_level(self, log_level):
        self.log_level = log_level
        if self.log_level <= LOG_INFO:
            print(f'[ Info ] Log level is set to {_log_level_dict[self.log_level]}.')
        
    def connect(self):
        self.serial_conn_obj = serial.Serial(self.uart_port_info['port'])
        ser_settings = self.serial_conn_obj.getSettingsDict()
        ser_settings.update(self.uart_port_info)
        self.serial_conn_obj.applySettingsDict(ser_settings)
        
        if self.log_level <= LOG_INFO:
            print(f"[ Info ] Serial connection is opened for port {self.uart_port_info['port']}")
        
        if self.log_file_path is not None:
            self.log_file_obj = open(self.log_file_path, 'ab+')
            if self.log_level <= LOG_INFO:
                print('[ Info ] Uart log file opened.')
            
        # create logging for serial_log_obj
        self.file_descriptor_process = fdpexpect.fdspawn(self.serial_conn_obj, logfile=self.log_file_obj, use_poll=True)
        if self.log_level <= LOG_INFO:
            print("[ Info ] File descriptor process is opened.\n")

    def consume_pending(self, expected_string, timeout=1):
        if not self.file_descriptor_process:
            raise UartSetupIssue("UART is not connected.")
        try:
            self.file_descriptor_process.expect([expected_string, pexpect.TIMEOUT, pexpect.EOF], timeout)
        except Exception:
            pass
    
    def disconnect(self):
        try:
            if self.file_descriptor_process and self.file_descriptor_process.isalive():
                self.file_descriptor_process.close()
                if self.log_level <= LOG_INFO:
                    print('[ Info ] File descriptor process is closed.')
            if self.serial_conn_obj and self.serial_conn_obj.isOpen():
                self.serial_conn_obj.close()
                if self.log_level <= LOG_INFO:
                    print('\n[ Info ] Serial connection obj is closed.')
            if self.log_file_obj:
                self.log_file_obj.close()
                if self.log_level <= LOG_INFO:
                    print('[ Info ] Uart log file closed.')
        except OSError as e:
            if self.log_level <= LOG_WARNING:
                print("[ Warning ] Error while closing UART resources :", e)
        except Exception as e:
            if self.log_level <= LOG_ERROR:
                print("[ Error ] Error while closing the UartInterface resources :", e)
                print(traceback.format_exc())
        finally:
            self.file_descriptor_process = None
            self.serial_conn_obj = None
            self.log_file_obj = None

    def run_command(self, cmd, expected_string, timeout=120, retry_count=1) -> str:
        if not self.file_descriptor_process:
            raise UartSetupIssue("UART is not connected.")

        last_error = None
        for iteration in range(retry_count):
            try:
                if self.log_level <= LOG_INFO:
                    print('[ Info ] Sending Uart Command : ' + str(cmd))
                self.file_descriptor_process.sendline(cmd)

                if self.log_level <= LOG_INFO:
                    print('[ Info ] Waiting for : ' + str(expected_string))
                index = self.file_descriptor_process.expect([expected_string, pexpect.TIMEOUT, pexpect.EOF], timeout)

                if index == 0:
                    response = self.file_descriptor_process.before + self.file_descriptor_process.after
                    if isinstance(response, bytes):
                        return response.decode(encoding='iso8859-1', errors='ignore')
                    return str(response)

                if index == 1:
                    last_error = UartSetupIssue(
                        f"[ Error ] Timeout while sending command: {cmd}. "
                        f"Expected '{expected_string}' within {timeout}s."
                    )
                    if self.log_level <= LOG_WARNING and iteration + 1 < retry_count:
                        print(
                            f"[ Warning ] Did not find expected_string in {timeout}s timeout. "
                            f"Trying again... iteration - {iteration + 1}"
                        )
                    continue

                raise UartSetupIssue(f"[ Error ] UART stream closed while sending command: {cmd}")
            except Exception as e:
                last_error = e

        raise last_error if last_error is not None else UartSetupIssue(
            f"[ Error ] Failed to execute UART command: {cmd}"
        )

    def send_command(self, cmd, expected_string=None, return_code=None, timeout=120, retry_count=1) -> bool:
        # command success status
        status = False
        
        try:
            index = None
            # retry 3 times
            for iteration in range(retry_count):
                # run the command
                if self.log_level <= LOG_INFO:
                    print('[ Info ] Sending Uart Command : ' + str(cmd))
                self.file_descriptor_process.sendline(cmd)
        
                # check the output if expected_string is defined
                # skip otherwise
                if expected_string is not None:
                    # check the constraint
                    if self.log_level <= LOG_INFO:
                        print('[ Info ] Waiting for : ' + expected_string)
                    index = self.file_descriptor_process.expect([expected_string, pexpect.TIMEOUT, pexpect.EOF], timeout)
                        
                    # checking the return code based on the list provided in expect()
                    if index == 0:  # Process is completes and we received expected string
                        # Print the DUT UART logs
                        cmd_response = self.file_descriptor_process.before + self.file_descriptor_process.after
                        cmd_response = cmd_response.decode(encoding='iso8859-1')
                        if self.log_level <= LOG_INFO:
                            print(f"[ Info ] Command Output \n```\n{cmd_response}\n```\n")
                        log_buffer = cmd_response
        
                    # if command execute successfully then break the loop
                    if index == 0:
                        if self.log_level <= LOG_INFO:
                            print(f'[ Info ] Successfully Executed Uart Command.\n')
                        break
                    elif index == 1:  # Timeout and we did not received expected string
                        if self.log_level <= LOG_WARNING:
                            print(
                                f"[ Warning ] Did not find expected_string in {timeout}s timeout. "
                                f"Trying again... iteration - {iteration + 1}"
                            )
        
            if index == 1:
                raise UartSetupIssue(f"[ Error ] Timeout while sending command : {cmd}")
            else:
                status = True
        except Exception as e:
            if self.log_level <= LOG_ERROR:
                print('[ Error ] Error occurred while sending command : ', e)
                print(traceback.format_exc())
        finally:
            pass
        
        return status

if __name__ == "__main__":
    # Example usage of Uart class
    uart = Uart('/dev/tty.usbmodem2101', log_file_path='uart_log.txt', log_level=LOG_INFO)
    uart.connect()
    
    # check if alreay in root shell
    if uart.send_command('ls', expected_string='home/', timeout=4): 
        print('Already in root shell')
    elif uart.send_command('root', expected_string='Welcome', timeout=4):
        print('Successfully entered root shell')
    
    import time
    uart.send_command('set_led 100 0 0', expected_string='LED set to RGB(100, 0, 0)', timeout=2)
    time.sleep(1) 
    uart.send_command('set_led 0 100 0', expected_string='LED set to RGB(0, 100, 0)', timeout=2)
    time.sleep(1) 
    uart.send_command('set_led 0 0 100', expected_string='LED set to RGB(0, 0, 100)', timeout=2)
    time.sleep(1) 

    # Reboot the device
    uart.send_command('reboot', expected_string='Rebooting...', timeout=4)
    time.sleep(2)  # wait for the device to reboot

    uart.disconnect()
