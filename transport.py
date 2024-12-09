"""Transport management for UART communication in Bartleby."""

import busio
import time
from constants import MESSAGE_TIMEOUT, BUFFER_CLEAR_TIMEOUT
from logging import log, TAG_TRANS

class TransportManager:
    """Manages shared UART instance for both text and MIDI communication"""
    def __init__(self, tx_pin, rx_pin, baudrate=31250, timeout=0.001):
        log(TAG_TRANS, "Initializing shared transport manager")
        try:
            log(TAG_TRANS, f"Configuring UART: baudrate={baudrate}, timeout={timeout}")
            self.uart = busio.UART(
                tx=tx_pin,
                rx=rx_pin,
                baudrate=baudrate,
                timeout=timeout,
                bits=8,
                parity=None,
                stop=1
            )
            self.uart_initialized = True
            log(TAG_TRANS, "UART configuration successful")
        except Exception as e:
            log(TAG_TRANS, f"Failed to initialize UART: {str(e)}", is_error=True)
            self.uart_initialized = False
            raise
        
    def get_uart(self):
        """Get the UART instance for text or MIDI use"""
        if not self.uart_initialized:
            log(TAG_TRANS, "Attempted to get UART before initialization", is_error=True)
            return None
        return self.uart
        
    def flush_buffers(self):
        """Clear any pending data in UART buffers"""
        if not self.uart_initialized:
            log(TAG_TRANS, "Skipping buffer flush - UART not initialized")
            return
            
        try:
            log(TAG_TRANS, "Flushing UART buffers")
            start_time = time.monotonic()
            while (time.monotonic() - start_time) < BUFFER_CLEAR_TIMEOUT:
                if self.uart and self.uart.in_waiting:
                    self.uart.read()
                else:
                    break
            log(TAG_TRANS, "Buffer flush complete")
        except Exception as e:
            log(TAG_TRANS, f"Error during buffer flush: {str(e)}", is_error=True)
        
    def cleanup(self):
        """Clean shutdown of transport"""
        if self.uart_initialized:
            log(TAG_TRANS, "Starting transport cleanup")
            try:
                self.flush_buffers()
                if self.uart:
                    self.uart.deinit()
                    log(TAG_TRANS, "UART deinitialized successfully")
            except Exception as e:
                log(TAG_TRANS, f"Error during cleanup: {str(e)}", is_error=True)
            finally:
                self.uart = None
                self.uart_initialized = False
                log(TAG_TRANS, "Transport cleanup complete")

class TextUart:
    """Handles text-based UART communication for receiving config only"""
    def __init__(self, uart):
        try:
            self.uart = uart
            self.buffer = bytearray()
            self.last_write = 0
            log(TAG_TRANS, "Text protocol initialized")
        except Exception as e:
            log(TAG_TRANS, f"Failed to initialize text protocol: {str(e)}", is_error=True)
            raise

    def write(self, message):
        """Write text message with minimum delay between writes"""
        try:
            current_time = time.monotonic()
            delay_needed = MESSAGE_TIMEOUT - (current_time - self.last_write)
            if delay_needed > 0:
                time.sleep(delay_needed)
                
            if isinstance(message, str):
                message = message.encode('utf-8')
            result = self.uart.write(message)
            self.last_write = time.monotonic()
            # Only log non-heartbeat messages by default
            if not message.startswith(b'\xe2\x99\xa1'):  # UTF-8 encoding of ♡
                log(TAG_TRANS, f"Wrote message of {len(message)} bytes")
            else:
                log(TAG_TRANS, "♡", is_heartbeat=True)
            return result
        except Exception as e:
            log(TAG_TRANS, f"Error writing message: {str(e)}", is_error=True)
            return 0

    def read(self):
        """Read available data and return complete messages, with improved resilience"""
        try:
            # If no data waiting, return None
            if not self.uart.in_waiting:
                return None

            # Read all available data
            data = self.uart.read()
            if not data:
                return None

            # Extend existing buffer
            self.buffer.extend(data)

            # Try to find a complete message (ending with newline)
            if b'\n' in self.buffer:
                # Split on first newline
                message, self.buffer = self.buffer.split(b'\n', 1)
                
                try:
                    # Attempt to decode and strip the message
                    decoded_message = message.decode('utf-8').strip()
                    
                    # Basic sanity check: message is not empty
                    if decoded_message:
                        # Check if it's a heartbeat message
                        if decoded_message.startswith('♡'):
                            log(TAG_TRANS, "♡", is_heartbeat=True)
                        else:
                            log(TAG_TRANS, f"Received complete message: {len(decoded_message)} chars")
                        return decoded_message
                except UnicodeDecodeError:
                    # If decoding fails, clear buffer to prevent accumulation of garbage
                    self.buffer = bytearray()
                    log(TAG_TRANS, "Received non-UTF8 data, buffer cleared", is_error=True)

            # No complete message, return None
            return None

        except Exception as e:
            # Catch any unexpected errors
            log(TAG_TRANS, f"Error in message reading: {str(e)}", is_error=True)
            # Clear buffer to prevent repeated errors
            self.buffer = bytearray()
            return None

    def clear_buffer(self):
        """Clear the internal buffer"""
        try:
            self.buffer = bytearray()
            log(TAG_TRANS, "Message buffer cleared")
        except Exception as e:
            log(TAG_TRANS, f"Error clearing buffer: {str(e)}", is_error=True)

    @property
    def in_waiting(self):
        try:
            return self.uart.in_waiting
        except Exception as e:
            log(TAG_TRANS, f"Error checking in_waiting: {str(e)}", is_error=True)
            return 0
