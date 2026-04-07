import os
import re
import time
import traceback
import serial

try:
    import pexpect
    from pexpect import fdpexpect
except Exception:  # pragma: no cover - pexpect is optional on Windows.
    pexpect = None
    fdpexpect = None

LOG_ERROR = 0
LOG_WARNING = 1
LOG_INFO = 2
LOG_NONE = 10

_log_level_dict = {
    LOG_NONE: 'NONE',
    LOG_ERROR: 'ERROR',
    LOG_WARNING: 'WARNING',
    LOG_INFO: 'INFO'
}

_EXPECT_TIMEOUT = pexpect.TIMEOUT if pexpect is not None else object()
_EXPECT_EOF = pexpect.EOF if pexpect is not None else object()


def _is_serial_open(serial_obj):
    if serial_obj is None:
        return False
    if hasattr(serial_obj, "is_open"):
        return bool(serial_obj.is_open)
    if hasattr(serial_obj, "isOpen"):
        return bool(serial_obj.isOpen())
    return False


class _SerialProcessAdapter:
    """Cross-platform expect-like adapter backed directly by pyserial."""

    def __init__(self, serial_conn_obj, logfile=None):
        self.serial_conn_obj = serial_conn_obj
        self.logfile = logfile
        self.before = b""
        self.after = b""
        self._alive = True

    def isalive(self):
        return self._alive and _is_serial_open(self.serial_conn_obj)

    def close(self):
        self._alive = False

    def sendline(self, cmd):
        payload = f"{cmd}{os.linesep}".encode("utf-8")
        self.serial_conn_obj.write(payload)
        self.serial_conn_obj.flush()
        if self.logfile:
            self.logfile.write(payload)
            self.logfile.flush()

    def expect(self, patterns, timeout=30):
        text_buffer = ""
        matcher_patterns = []
        timeout_index = None
        eof_index = None

        for index, pattern in enumerate(patterns):
            if pattern is _EXPECT_TIMEOUT:
                timeout_index = index
                continue
            if pattern is _EXPECT_EOF:
                eof_index = index
                continue
            if isinstance(pattern, bytes):
                matcher_patterns.append((index, re.compile(pattern.decode("iso8859-1", errors="ignore"))))
                continue
            if hasattr(pattern, "search"):
                matcher_patterns.append((index, pattern))
                continue
            matcher_patterns.append((index, re.compile(str(pattern))))

        timeout_s = 30.0 if timeout is None else float(timeout)
        if timeout_s <= 0:
            timeout_s = 0.05

        deadline = time.monotonic() + timeout_s
        original_timeout = self.serial_conn_obj.timeout
        self.serial_conn_obj.timeout = min(max(timeout_s, 0.05), 0.2)

        try:
            while True:
                for index, matcher in matcher_patterns:
                    match = matcher.search(text_buffer)
                    if match:
                        self.before = text_buffer[:match.start()].encode("iso8859-1", errors="ignore")
                        self.after = text_buffer[match.start():match.end()].encode("iso8859-1", errors="ignore")
                        return index

                if not self.isalive():
                    self.before = text_buffer.encode("iso8859-1", errors="ignore")
                    self.after = b""
                    if eof_index is not None:
                        return eof_index
                    raise EOFError("UART stream closed.")

                if time.monotonic() >= deadline:
                    self.before = text_buffer.encode("iso8859-1", errors="ignore")
                    self.after = b""
                    if timeout_index is not None:
                        return timeout_index
                    raise TimeoutError("UART read timed out.")

                try:
                    read_size = self.serial_conn_obj.in_waiting or 1
                except Exception:
                    read_size = 1
                chunk = self.serial_conn_obj.read(read_size)
                if not chunk:
                    continue
                if isinstance(chunk, str):
                    chunk = chunk.encode("iso8859-1", errors="ignore")
                if self.logfile:
                    self.logfile.write(chunk)
                    self.logfile.flush()
                text_buffer += chunk.decode("iso8859-1", errors="ignore")
        finally:
            self.serial_conn_obj.timeout = original_timeout


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

    @staticmethod
    def _use_fdspawn_backend():
        return os.name != "nt" and fdpexpect is not None

    def __del__(self):
        self.disconnect()
        if self.log_level <= LOG_INFO:
            print('[ Info ] Deleting UartInterface object and releasing the resources.')

    def set_log_level(self, log_level):
        self.log_level = log_level
        if self.log_level <= LOG_INFO:
            print(f'[ Info ] Log level is set to {_log_level_dict[self.log_level]}.')

    def connect(self):
        self.serial_conn_obj = serial.Serial(**self.uart_port_info)

        if self.log_level <= LOG_INFO:
            print(f"[ Info ] Serial connection is opened for port {self.uart_port_info['port']}")

        if self.log_file_path is not None:
            self.log_file_obj = open(self.log_file_path, 'ab+')
            if self.log_level <= LOG_INFO:
                print('[ Info ] Uart log file opened.')

        # `fdpexpect` works well on POSIX. On Windows we use a serial adapter with
        # compatible `sendline/expect` semantics.
        if self._use_fdspawn_backend():
            self.file_descriptor_process = fdpexpect.fdspawn(
                self.serial_conn_obj,
                logfile=self.log_file_obj,
                use_poll=True
            )
        else:
            self.file_descriptor_process = _SerialProcessAdapter(
                self.serial_conn_obj,
                logfile=self.log_file_obj
            )
        if self.log_level <= LOG_INFO:
            print("[ Info ] File descriptor process is opened.\n")

    def consume_pending(self, expected_string, timeout=1):
        if not self.file_descriptor_process:
            raise UartSetupIssue("UART is not connected.")
        try:
            self.file_descriptor_process.expect([expected_string, _EXPECT_TIMEOUT, _EXPECT_EOF], timeout)
        except Exception:
            pass

    def disconnect(self):
        try:
            if self.file_descriptor_process and self.file_descriptor_process.isalive():
                self.file_descriptor_process.close()
                if self.log_level <= LOG_INFO:
                    print('[ Info ] File descriptor process is closed.')
            if _is_serial_open(self.serial_conn_obj):
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
                index = self.file_descriptor_process.expect([expected_string, _EXPECT_TIMEOUT, _EXPECT_EOF], timeout)

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
                    index = self.file_descriptor_process.expect([expected_string, _EXPECT_TIMEOUT, _EXPECT_EOF], timeout)

                    # checking the return code based on the list provided in expect()
                    if index == 0:  # Process is completes and we received expected string
                        # Print the DUT UART logs
                        cmd_response = self.file_descriptor_process.before + self.file_descriptor_process.after
                        cmd_response = cmd_response.decode(encoding='iso8859-1', errors='ignore')
                        if self.log_level <= LOG_INFO:
                            print(f"[ Info ] Command Output \n```\n{cmd_response}\n```\n")

                    # if command execute successfully then break the loop
                    if index == 0:
                        if self.log_level <= LOG_INFO:
                            print('[ Info ] Successfully Executed Uart Command.\n')
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

        return status


if __name__ == "__main__":
    # Example usage of Uart class
    default_port = 'COM3' if os.name == 'nt' else '/dev/tty.usbmodem1101'
    
    uart = Uart(default_port, log_file_path='uart_log.txt', log_level=LOG_INFO)
    uart.connect()

    uart.send_command('auto dut list', expected_string='Supported DUTs:.*=>', timeout=2)
    time.sleep(1)

    uart.send_command('auto set dut j722s-evm', expected_string='DUT initialized.*=>', timeout=2)
    time.sleep(1)

    uart.send_command('auto measure power 10 10', expected_string='Total .*=>', timeout=2)
    time.sleep(1)

    uart.disconnect()
