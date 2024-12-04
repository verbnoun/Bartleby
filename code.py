import board
import busio
import digitalio
import time
from hardware import (
    Multiplexer, KeyboardHandler, RotaryEncoderHandler, 
    PotentiometerHandler, Constants as HWConstants
)
from midi import MidiLogic

class Constants:
    DEBUG = True
    SEE_HEARTBEAT = False
    
    # Hardware Setup
    SETUP_DELAY = 0.1
    
    # UART/MIDI Pins
    UART_TX = board.GP16
    UART_RX = board.GP17
    
    # Connection
    DETECT_PIN = board.GP22
    COMMUNICATION_TIMEOUT = 5.0
    
    # New Connection Constants
    STARTUP_DELAY = 1.0  # Give devices time to initialize
    RETRY_DELAY = 0.25   # Delay between connection attempts
    ERROR_RECOVERY_DELAY = 0.5  # Delay after errors before retry
    BUFFER_CLEAR_TIMEOUT = 0.1  # Time to wait for buffer clearing
    MAX_CONFIG_RETRIES = 3
    
    # Timing Intervals
    POT_SCAN_INTERVAL = 0.02
    ENCODER_SCAN_INTERVAL = 0.001
    MAIN_LOOP_INTERVAL = 0.001
    MESSAGE_TIMEOUT = 0.05

    # Message Types
    MSG_HELLO = "hello"
    MSG_CONFIG = "cc:"
    
    # MIDI Settings
    UART_BAUDRATE = 31250
    UART_TIMEOUT = 0.001
    
    # MIDI CC for Handshake
    HANDSHAKE_CC = 119  # Undefined CC number
    HANDSHAKE_VALUE = 42  # Arbitrary value

    # Timing precision
    TIME_PRECISION = 9  # Nanosecond precision

def get_precise_time():
    """Get high precision time measurement in nanoseconds"""
    return time.monotonic_ns()

def format_processing_time(start_time, operation=None):
    """Format processing time with nanosecond precision and optional operation description"""
    elapsed_ns = get_precise_time() - start_time
    elapsed_ms = elapsed_ns / 1_000_000  # Convert ns to ms
    if operation:
        return f"{operation} took {elapsed_ms:.3f}ms"
    return f"Processing took {elapsed_ms:.3f}ms"

class TransportManager:
    """Manages shared UART instance for both text and MIDI communication"""
    def __init__(self, tx_pin, rx_pin, baudrate=31250, timeout=0.001):
        print("Initializing shared transport...")
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
        print("Transport initialized")
        
    def get_uart(self):
        """Get the UART instance for text or MIDI use"""
        return self.uart
        
    def flush_buffers(self):
        """Clear any pending data in UART buffers"""
        if not self.uart_initialized:
            return
        try:
            start_time = get_precise_time()
            while (get_precise_time() - start_time) < (Constants.BUFFER_CLEAR_TIMEOUT * 1_000_000_000):  # Convert to ns
                if self.uart and self.uart.in_waiting:
                    self.uart.read()
                else:
                    break
        except Exception:
            # If we hit an error trying to flush, the UART is likely already deinitialized
            pass
        
    def cleanup(self):
        """Clean shutdown of transport"""
        if self.uart_initialized:
            try:
                self.flush_buffers()
                if self.uart:
                    self.uart.deinit()
            except Exception:
                # If we hit an error, the UART is likely already deinitialized
                pass
            finally:
                self.uart = None
                self.uart_initialized = False

class TextUart:
    """Handles text-based UART communication for receiving config only"""
    def __init__(self, uart):
        self.uart = uart
        self.buffer = bytearray()
        self.last_write = 0
        print("Text protocol initialized")

    def write(self, message):
        """Write text message with minimum delay between writes"""
        current_time = get_precise_time()
        delay_needed = (Constants.MESSAGE_TIMEOUT * 1_000_000_000) - (current_time - self.last_write)  # Convert to ns
        if delay_needed > 0:
            time.sleep(delay_needed / 1_000_000_000)  # Convert back to seconds
            
        if isinstance(message, str):
            message = message.encode('utf-8')
        result = self.uart.write(message)
        self.last_write = get_precise_time()
        return result

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
                        return decoded_message
                except UnicodeDecodeError:
                    # If decoding fails, clear buffer to prevent accumulation of garbage
                    self.buffer = bytearray()
                    print("Received non-UTF8 data, buffer cleared")

            # No complete message, return None
            return None

        except Exception as e:
            # Catch any unexpected errors
            print(f"Error in message reading: {e}")
            # Clear buffer to prevent repeated errors
            self.buffer = bytearray()
            return None

    def clear_buffer(self):
        """Clear the internal buffer"""
        self.buffer = bytearray()

    @property
    def in_waiting(self):
        try:
            return self.uart.in_waiting
        except Exception:
            return 0

class BartlebyConnectionManager:
    """
    Manages connection state and handshake protocol for Bartleby (Base Station).
    Receives text messages, sends MIDI responses.
    """
    # States
    STANDALONE = 0      # No active client
    HANDSHAKING = 1     # In handshake process
    CONNECTED = 2       # Fully connected and operational
    
    def __init__(self, text_uart, hardware_coordinator, midi_logic, transport_manager):
        self.uart = text_uart
        self.hardware = hardware_coordinator
        self.midi = midi_logic
        self.transport = transport_manager
        
        # Connection state
        self.state = self.STANDALONE
        self.last_message_time = 0
        self.config_received = False
        
        # Store CC mapping with names
        self.cc_mapping = {}  # Format: {pot_number: {'cc': cc_number, 'name': cc_name}}
        
        print("Bartleby connection manager initialized - listening for Candide")
        
    def update_state(self):
        """Check for timeouts and manage state transitions"""
        if self.state != self.STANDALONE:
            current_time = get_precise_time()
            if (current_time - self.last_message_time) > (Constants.COMMUNICATION_TIMEOUT * 1_000_000_000):  # Convert to ns
                print("Communication timeout - returning to standalone")
                self._reset_state()
                
    def handle_message(self, message):
        """Process incoming text messages"""
        if not message:
            return
            
        # Update last message time for timeout tracking
        self.last_message_time = get_precise_time()
        
        # Handle hello message
        if message.startswith("hello"):
            if self.state == self.STANDALONE:
                print("Hello received - starting handshake")
                self.transport.flush_buffers()
                self.state = self.HANDSHAKING
                self._send_handshake_cc()
            elif self.state == self.HANDSHAKING:
                # Send handshake CC every time hello is received during handshaking
                print("Hello received during handshake")
                self._send_handshake_cc()
            return
            
        # Handle config message during handshake
        if self.state == self.HANDSHAKING and message.startswith("cc:"):
            print("Config received - parsing CC mapping")
            self._parse_cc_config(message)
            self.config_received = True
            self.state = self.CONNECTED
            self._send_current_hw_state()
            
            # Debug output of CC mapping if DEBUG is true
            if Constants.DEBUG:
                print("\nReceived CC Configuration:")
                for pot_num, mapping in self.cc_mapping.items():
                    print(f"Pot {pot_num}: CC {mapping['cc']} - {mapping['name']}")
                print()  # Extra newline for readability
            return
            
        # Handle config message after connection established
        if self.state == self.CONNECTED and message.startswith("cc:"):
            print("Config update received - applying new CC mapping")
            self._parse_cc_config(message)
            # Pass the new CC configuration to MIDI logic
            self.midi.handle_config_message(message)
            # Send current pot values after receiving new config
            self._send_current_hw_state()
            # Debug output of CC mapping if DEBUG is true
            if Constants.DEBUG:
                print("\nUpdated CC Configuration:")
                for pot_num, mapping in self.cc_mapping.items():
                    print(f"Pot {pot_num}: CC {mapping['cc']} - {mapping['name']}")
                print()  # Extra newline for readability
            return
            
        # Handle heartbeat in connected state
        if self.state == self.CONNECTED and message.startswith("♥︎"):
            if Constants.SEE_HEARTBEAT and Constants.DEBUG:
                print(f"♥︎")
            return  # Just update last_message_time
            
    def _parse_cc_config(self, message):
        """Parse CC configuration message with names"""
        self.cc_mapping.clear()  # Clear existing mapping
        
        # Remove "cc:" prefix
        config_part = message[3:]
        if not config_part:
            return
            
        # Split into individual assignments
        assignments = config_part.split(',')
        for assignment in assignments:
            if not assignment:
                continue
                
            # Parse pot=cc:name format
            try:
                pot_part, rest = assignment.split('=')
                cc_part, name = rest.split(':') if ':' in rest else (rest, f"CC{rest}")
                
                pot_num = int(pot_part)
                cc_num = int(cc_part)
                
                self.cc_mapping[pot_num] = {
                    'cc': cc_num,
                    'name': name
                }
            except (ValueError, IndexError) as e:
                print(f"Error parsing CC assignment '{assignment}': {e}")
                continue
            
    def _send_handshake_cc(self):
        """Send handshake CC message"""
        try:
            message = [0xB0, Constants.HANDSHAKE_CC, Constants.HANDSHAKE_VALUE]
            self.midi.message_sender.send_message(message)
            print("Handshake CC sent")
        except Exception as e:
            print(f"Failed to send handshake CC: {str(e)}")
            self._reset_state()
            
    def _send_current_hw_state(self):
        """Send current hardware state as MIDI messages"""
        try:
            start_time = get_precise_time()
            
            # Force read of all pots during handshake
            all_pots = self.hardware.components['pots'].read_all_pots()
            
            # Convert all_pots to the format expected by midi.update()
            pot_changes = [(pot_index, 0, normalized_value) for pot_index, normalized_value in all_pots]
            
            # Send pot values
            if pot_changes:
                self.midi.update([], pot_changes, {})
                print(f"Sent {len(pot_changes)} pot values")
            
            # Send encoder position
            encoder_pos = self.hardware.components['encoders'].get_encoder_position(0)
            if encoder_pos != 0:
                self.midi.handle_octave_shift(encoder_pos)
                print(f"Current octave position sent: {encoder_pos}")
            
            # Pass the CC configuration to MIDI logic
            if self.cc_mapping:
                config_message = "cc:" + ",".join(["{0}={1}:{2}".format(pot, mapping["cc"], mapping["name"]) for pot, mapping in self.cc_mapping.items()])
                self.midi.handle_config_message(config_message)
                print("Sent CC configuration to MIDI logic")
                
            print(format_processing_time(start_time, "Hardware state synchronization"))
                
        except Exception as e:
            print(f"Failed to send hardware state: {str(e)}")
            
    def _reset_state(self):
        """Reset to initial state"""
        self.state = self.STANDALONE
        self.config_received = False
        self.last_message_time = 0
        self.cc_mapping.clear()
        self.transport.flush_buffers()
        
    def cleanup(self):
        """Clean up resources"""
        self._reset_state()
        
    def is_connected(self):
        """Check if fully connected"""
        return self.state == self.CONNECTED

class StateManager:
    def __init__(self):
        self.current_time = 0
        self.last_pot_scan = 0
        self.last_encoder_scan = 0
        
    def update_time(self):
        self.current_time = get_precise_time()
        
    def should_scan_pots(self):
        return (self.current_time - self.last_pot_scan) >= (Constants.POT_SCAN_INTERVAL * 1_000_000_000)  # Convert to ns
        
    def should_scan_encoders(self):
        return (self.current_time - self.last_encoder_scan) >= (Constants.ENCODER_SCAN_INTERVAL * 1_000_000_000)  # Convert to ns
        
    def update_pot_scan_time(self):
        self.last_pot_scan = self.current_time
        
    def update_encoder_scan_time(self):
        self.last_encoder_scan = self.current_time

class HardwareCoordinator:
    def __init__(self):
        print("Setting up hardware...")
        # Set up detect pin as output HIGH to signal presence
        self.detect_pin = digitalio.DigitalInOut(Constants.DETECT_PIN)
        self.detect_pin.direction = digitalio.Direction.OUTPUT
        self.detect_pin.value = True
        
        # Initialize components
        self.components = self._initialize_components()
        time.sleep(Constants.SETUP_DELAY)
        
    def _initialize_components(self):
        control_mux = Multiplexer(
            HWConstants.CONTROL_MUX_SIG,
            HWConstants.CONTROL_MUX_S0,
            HWConstants.CONTROL_MUX_S1,
            HWConstants.CONTROL_MUX_S2,
            HWConstants.CONTROL_MUX_S3
        )
        
        keyboard = self._setup_keyboard()
        encoders = RotaryEncoderHandler(
            HWConstants.OCTAVE_ENC_CLK,
            HWConstants.OCTAVE_ENC_DT
        )
        
        return {
            'control_mux': control_mux,
            'keyboard': keyboard,
            'encoders': encoders,
            'pots': PotentiometerHandler(control_mux)
        }
    
    def _setup_keyboard(self):
        keyboard_l1a = Multiplexer(
            HWConstants.KEYBOARD_L1A_MUX_SIG,
            HWConstants.KEYBOARD_L1A_MUX_S0,
            HWConstants.KEYBOARD_L1A_MUX_S1,
            HWConstants.KEYBOARD_L1A_MUX_S2,
            HWConstants.KEYBOARD_L1A_MUX_S3
        )
        
        keyboard_l1b = Multiplexer(
            HWConstants.KEYBOARD_L1B_MUX_SIG,
            HWConstants.KEYBOARD_L1B_MUX_S0,
            HWConstants.KEYBOARD_L1B_MUX_S1,
            HWConstants.KEYBOARD_L1B_MUX_S2,
            HWConstants.KEYBOARD_L1B_MUX_S3
        )
        
        return KeyboardHandler(
            keyboard_l1a,
            keyboard_l1b,
            HWConstants.KEYBOARD_L2_MUX_S0,
            HWConstants.KEYBOARD_L2_MUX_S1,
            HWConstants.KEYBOARD_L2_MUX_S2,
            HWConstants.KEYBOARD_L2_MUX_S3
        )
    
    def read_hardware_state(self, state_manager):
        changes = {
            'keys': [],
            'pots': [],
            'encoders': []
        }
        
        # Always read keys at full speed
        start_time = get_precise_time()
        changes['keys'] = self.components['keyboard'].read_keys()
        if changes['keys']:
            print(format_processing_time(start_time, "Key state read"))
        
        # Read pots at interval
        if state_manager.should_scan_pots():
            start_time = get_precise_time()
            changes['pots'] = self.components['pots'].read_pots()
            if changes['pots']:
                print(format_processing_time(start_time, "Potentiometer scan"))
            state_manager.update_pot_scan_time()
        
        # Read encoders at interval
        if state_manager.should_scan_encoders():
            start_time = get_precise_time()
            for i in range(self.components['encoders'].num_encoders):
                new_events = self.components['encoders'].read_encoder(i)
                if new_events:
                    changes['encoders'].extend(new_events)
            if changes['encoders']:
                print(format_processing_time(start_time, "Encoder scan"))
            state_manager.update_encoder_scan_time()
            
        return changes
    
    def handle_encoder_events(self, encoder_events, midi):
        start_time = get_precise_time()
        for event in encoder_events:
            if event[0] == 'rotation':
                _, direction = event[1:3]
                midi.handle_octave_shift(direction)
                if Constants.DEBUG:
                    print(f"Octave shifted {direction}: new position {self.components['encoders'].get_encoder_position(0)}")
                    print(format_processing_time(start_time, "Octave shift processing"))
    
    def reset_encoders(self):
        self.components['encoders'].reset_all_encoder_positions()

class Bartleby:
    def __init__(self):
        print("\nWake Up Bartleby!")
        self.state_manager = StateManager()
        
        # Initialize shared transport first
        self.transport = TransportManager(
            tx_pin=Constants.UART_TX,
            rx_pin=Constants.UART_RX,
            baudrate=Constants.UART_BAUDRATE,
            timeout=Constants.UART_TIMEOUT
        )
        
        # Get shared UART for text and MIDI
        shared_uart = self.transport.get_uart()
        self.text_uart = TextUart(shared_uart)
        self.midi = MidiLogic(
            transport_manager=self.transport,
            midi_callback=self._handle_midi_config
        )
        
        # Initialize hardware first to set up detect pin
        self.hardware = HardwareCoordinator()
        
        # Initialize connection manager with hardware's detect pin
        self.connection_manager = BartlebyConnectionManager(
            self.text_uart,
            self.hardware,
            self.midi,
            self.transport
        )
        
        self._setup_initial_state()

    def _handle_midi_config(self, message):
        self.connection_manager.handle_message(message)

    def _setup_initial_state(self):
        self.hardware.reset_encoders()
        
        # Force read of all pots during initialization
        initial_pots = self.hardware.components['pots'].read_all_pots()
        
        # Optional: Send initial pot values during initialization
        if initial_pots and Constants.DEBUG:
            print(f"Initial pot values read: {initial_pots}")
        
        # Add startup delay to ensure both sides are ready
        time.sleep(Constants.STARTUP_DELAY)
        print("\nBartleby (v1.0) is awake... (◕‿◕✿)")

    def update(self):
        try:
            # Update current time
            self.state_manager.update_time()
            
            # Check connection states
            self.connection_manager.update_state()
            
            # Process hardware and MIDI
            start_time = get_precise_time()
            changes = self.hardware.read_hardware_state(self.state_manager)
            
            # In Bartleby.update():
            if self.text_uart.in_waiting:
                message = self.text_uart.read()
                if message:
                    try:
                        if Constants.DEBUG and not message.startswith('♥︎'):
                            print(f"Received message: '{message}'")
                        self.connection_manager.handle_message(message)
                    except Exception as e:
                        if str(e):
                            print(f"Received non-text data: {message}")

            # Handle encoder events and MIDI updates
            if changes['encoders']:
                self.hardware.handle_encoder_events(changes['encoders'], self.midi)
            
            # Process MIDI updates with timing
            if changes['keys'] or changes['pots'] or changes['encoders']:
                # Log total time from hardware detection to MIDI sent
                hw_detect_time = get_precise_time()
                self.midi.update(
                    changes['keys'],
                    changes['pots'],
                    {}  # Empty config since we're not using instrument settings
                )
                print(format_processing_time(start_time, "Total time"))
            
            return True
                
        except KeyboardInterrupt:
            return False
        except Exception as e:
            print(f"Error in main loop: {str(e)}")
            return False

    def run(self):
        print("Starting main loop...")
        try:
            while self.update():
                time.sleep(Constants.MAIN_LOOP_INTERVAL)
        finally:
            self.cleanup()

    def cleanup(self):
        print("Starting cleanup sequence...")
        if hasattr(self.hardware, 'detect_pin'):
            print("Cleaning up hardware...")
            self.hardware.detect_pin.value = False
            self.hardware.detect_pin.deinit()
        if self.connection_manager:
            self.connection_manager.cleanup()
        if self.midi:
            print("Cleaning up MIDI...")
            self.midi.cleanup()
        if self.transport:
            print("Cleaning up transport...")
            self.transport.cleanup()
        print("\nBartleby goes to sleep... ( ◡_◡)ᶻ 𝗓 𐰁")

    def play_greeting(self):
        """Play greeting chime using MPE"""
        if Constants.DEBUG:
            print("Playing MPE greeting sequence")
            
        base_key_id = -1
        base_pressure = 0.75
        
        greeting_notes = [60, 64, 67, 72]
        velocities = [0.6, 0.7, 0.8, 0.9]
        durations = [0.2, 0.2, 0.2, 0.4]
        
        for idx, (note, velocity, duration) in enumerate(zip(greeting_notes, velocities, durations)):
            key_id = base_key_id - idx
            channel = self.midi.channel_manager.allocate_channel(key_id)
            note_state = self.midi.channel_manager.add_note(key_id, note, channel, int(velocity * 127))
            
            # Send in MPE order: CC74 → Pressure → Pitch Bend → Note On
            self.midi.message_sender.send_message([0xB0 | channel, Constants.CC_TIMBRE, Constants.TIMBRE_CENTER])
            self.midi.message_sender.send_message([0xD0 | channel, int(base_pressure * 127)])
            self.midi.message_sender.send_message([0xE0 | channel, 0x00, 0x40])  # Center pitch bend
            self.midi.message_sender.send_message([0x90 | channel, note, int(velocity * 127)])
            
            time.sleep(duration)
            
            self.midi.message_sender.send_message([0xD0 | channel, 0])  # Zero pressure
            self.midi.message_sender.send_message([0x80 | channel, note, 0])
            self.midi.channel_manager.release_note(key_id)
            
            time.sleep(0.05)

def main():
    controller = Bartleby()
    controller.run()

if __name__ == "__main__":
    main()
