#!/usr/bin/env python3
"""
Py400kb USB HID Forwarder
Forwards keyboard and mouse input from pix00 computers to USB gadget mode
Supports Pi 400, Pi 500, and Pi 500+
"""

import argparse
import errno
import fcntl
import os
import signal
import struct
import sys
import time
import subprocess
from pathlib import Path
from typing import Optional, Tuple

# Additional errno constants for handling specific errors
EPIPE = 32
ESHUTDOWN = 108 # Usually destination PC not connected to USB cable

# HID Report Descriptor combining keyboard and mouse
REPORT_DESC = bytes([
    # Keyboard Report (Report ID 1)
    0x05, 0x01,        # Usage Page (Generic Desktop Ctrls)
    0x09, 0x06,        # Usage (Keyboard)
    0xA1, 0x01,        # Collection (Application)
    0x85, 0x01,        #   Report ID (1)
    0x05, 0x07,        #   Usage Page (Kbrd/Keypad)
    0x19, 0xE0,        #   Usage Minimum (0xE0)
    0x29, 0xE7,        #   Usage Maximum (0xE7)
    0x15, 0x00,        #   Logical Minimum (0)
    0x25, 0x01,        #   Logical Maximum (1)
    0x75, 0x01,        #   Report Size (1)
    0x95, 0x08,        #   Report Count (8)
    0x81, 0x02,        #   Input (Data,Var,Abs)
    0x95, 0x01,        #   Report Count (1)
    0x75, 0x08,        #   Report Size (8)
    0x81, 0x01,        #   Input (Const,Array,Abs)
    0x95, 0x03,        #   Report Count (3)
    0x75, 0x01,        #   Report Size (1)
    0x05, 0x08,        #   Usage Page (LEDs)
    0x19, 0x01,        #   Usage Minimum (Num Lock)
    0x29, 0x03,        #   Usage Maximum (Scroll Lock)
    0x91, 0x02,        #   Output (Data,Var,Abs)
    0x95, 0x05,        #   Report Count (5)
    0x75, 0x01,        #   Report Size (1)
    0x91, 0x01,        #   Output (Const,Array,Abs)
    0x95, 0x06,        #   Report Count (6)
    0x75, 0x08,        #   Report Size (8)
    0x15, 0x00,        #   Logical Minimum (0)
    0x26, 0xFF, 0x00,  #   Logical Maximum (255)
    0x05, 0x07,        #   Usage Page (Kbrd/Keypad)
    0x19, 0x00,        #   Usage Minimum (0x00)
    0x2A, 0xFF, 0x00,  #   Usage Maximum (0xFF)
    0x81, 0x00,        #   Input (Data,Array,Abs)
    0xC0,              # End Collection

    # Mouse Report (Report ID 2)
    0x05, 0x01,        # Usage Page (Generic Desktop Ctrls)
    0x09, 0x02,        # Usage (Mouse)
    0xA1, 0x01,        # Collection (Application)
    0x85, 0x02,        #   Report ID (2)
    0x09, 0x01,        #   Usage (Pointer)
    0xA1, 0x00,        #   Collection (Physical)
    0x05, 0x09,        #     Usage Page (Button)
    0x19, 0x01,        #     Usage Minimum (0x01)
    0x29, 0x03,        #     Usage Maximum (0x03)
    0x15, 0x00,        #     Logical Minimum (0)
    0x25, 0x01,        #     Logical Maximum (1)
    0x75, 0x01,        #     Report Size (1)
    0x95, 0x03,        #     Report Count (3)
    0x81, 0x02,        #     Input (Data,Var,Abs)
    0x75, 0x05,        #     Report Size (5)
    0x95, 0x01,        #     Report Count (1)
    0x81, 0x01,        #     Input (Const,Array,Abs)
    0x05, 0x01,        #     Usage Page (Generic Desktop Ctrls)
    0x09, 0x30,        #     Usage (X)
    0x09, 0x31,        #     Usage (Y)
    0x09, 0x38,        #     Usage (Wheel)
    0x15, 0x81,        #     Logical Minimum (-127)
    0x25, 0x7F,        #     Logical Maximum (127)
    0x75, 0x08,        #     Report Size (8)
    0x95, 0x03,        #     Report Count (3)
    0x81, 0x06,        #     Input (Data,Var,Rel)
    0xC0,              #   End Collection
    0xC0,              # End Collection
])

KEYBOARD_HID_REPORT_SIZE = 8 # constant from pi400kb
MOUSE_HID_REPORT_SIZE = 4 # constant from pi400kb

# IOCTL constants for input device grabbing
EVIOCGRAB = 0x40044590

# Device configurations for different Pi models 400, 500 & 500+
DEVICE_CONFIGS = {
    'pi400': {
        'keyboard_vid': 0x04d9,
        'keyboard_pid': 0x0007,
        'keyboard_dev': '/dev/input/by-id/usb-_Raspberry_Pi_Internal_Keyboard-event-kbd',
        'mouse_vid': 0x093a,
        'mouse_pid': 0x2510,
        'mouse_dev': '/dev/input/by-id/usb-PixArt_USB_Optical_Mouse-event-mouse',
    },
    'pi500': {
        'keyboard_vid': 0x2e8a,
        'keyboard_pid': 0x0010,
        'keyboard_dev': '/dev/input/by-id/usb-Raspberry_Pi_Ltd_Pi_500_Keyboard-event-kbd',
        'mouse_vid': 0x093a,
        'mouse_pid': 0x2510,
        'mouse_dev': '/dev/input/by-id/usb-PixArt_USB_Optical_Mouse-event-mouse',
    },
    'pi500plus': {
        'keyboard_vid': 0x0000,
        'keyboard_pid': 0x0000,
        'keyboard_dev': '/dev/input/by-id/PLACEHOLDER',
        'mouse_vid': 0x0000,
        'mouse_pid': 0x0000,
        'mouse_dev': '/dev/input/by-id/PLACEHOLDER',
    }
}


class USBGadget:
    """Manages USB Gadget configuration using ConfigFS"""
    
    def __init__(self, configfs_path='/sys/kernel/config'):
        self.configfs_path = Path(configfs_path)
        self.gadget_path = self.configfs_path / 'usb_gadget' / 'g1'
        self.enabled = False
        
    def init(self, vid: int, pid: int) -> bool:
        """Initialize USB gadget"""
        try:
            # Load libcomposite module
            self._modprobe_libcomposite()
            
            # Create gadget directory
            self.gadget_path.mkdir(parents=True, exist_ok=True)
            
            # Set USB device descriptor
            self._write_file(self.gadget_path / 'idVendor', f'0x{vid:04x}')
            self._write_file(self.gadget_path / 'idProduct', f'0x{pid:04x}')
            self._write_file(self.gadget_path / 'bcdDevice', '0x0001')
            self._write_file(self.gadget_path / 'bcdUSB', '0x0200')
            self._write_file(self.gadget_path / 'bDeviceClass', '0x00')
            self._write_file(self.gadget_path / 'bDeviceSubClass', '0x00')
            self._write_file(self.gadget_path / 'bDeviceProtocol', '0x00')
            self._write_file(self.gadget_path / 'bMaxPacketSize0', '64')
            
            # Set strings
            strings_path = self.gadget_path / 'strings' / '0x409'
            strings_path.mkdir(parents=True, exist_ok=True)
            self._write_file(strings_path / 'serialnumber', '0123456789')
            self._write_file(strings_path / 'manufacturer', 'Gadgetoid')
            self._write_file(strings_path / 'product', 'Pi400KB')
            
            # Create HID function
            func_path = self.gadget_path / 'functions' / 'hid.usb0'
            func_path.mkdir(parents=True, exist_ok=True)
            self._write_file(func_path / 'protocol', '1')
            self._write_file(func_path / 'subclass', '0')
            self._write_file(func_path / 'report_length', '16')
            
            # Write HID report descriptor
            with open(func_path / 'report_desc', 'wb') as f:
                f.write(REPORT_DESC)
            
            # Create configuration
            config_path = self.gadget_path / 'configs' / 'c.1'
            config_path.mkdir(parents=True, exist_ok=True)
            self._write_file(config_path / 'MaxPower', '250')
            
            config_strings_path = config_path / 'strings' / '0x409'
            config_strings_path.mkdir(parents=True, exist_ok=True)
            self._write_file(config_strings_path / 'configuration', '1xHID')
            
            # Link function to configuration
            link_path = config_path / 'hid.usb0'
            if not link_path.exists():
                link_path.symlink_to(func_path)
            
            # Enable gadget
            udc = self._find_udc()
            if udc:
                self._write_file(self.gadget_path / 'UDC', udc)
                self.enabled = True
                print(f"USB Gadget enabled on UDC: {udc}")
                return True
            else:
                print("Error: No UDC found")
                return False
                
        except Exception as e:
            print(f"Error initializing USB gadget: {e}")
            return False
    
    def cleanup(self):
        """Clean up USB gadget configuration"""
        if not self.gadget_path.exists():
            return
            
        try:
            # Disable gadget
            if self.enabled:
                self._write_file(self.gadget_path / 'UDC', '')
                self.enabled = False
            
            # Remove symlinks
            config_path = self.gadget_path / 'configs' / 'c.1'
            link_path = config_path / 'hid.usb0'
            if link_path.exists():
                link_path.unlink()
            
            # Remove directories (must be in reverse order)
            self._rmdir_recursive(config_path / 'strings' / '0x409')
            self._rmdir_recursive(config_path)
            self._rmdir_recursive(self.gadget_path / 'functions' / 'hid.usb0')
            self._rmdir_recursive(self.gadget_path / 'strings' / '0x409')
            self._rmdir_recursive(self.gadget_path)
            
            print("USB Gadget cleaned up")
        except Exception as e:
            print(f"Error cleaning up USB gadget: {e}")
    
    def _modprobe_libcomposite(self):
        """Load libcomposite kernel module"""
        try:
            subprocess.run(['modprobe', 'libcomposite'], check=False)
        except Exception as e:
            print(f"Warning: Could not load libcomposite module: {e}")
    
    def _find_udc(self) -> Optional[str]:
        """Find available UDC (USB Device Controller)"""
        udc_path = Path('/sys/class/udc')
        if udc_path.exists():
            udcs = list(udc_path.iterdir())
            if udcs:
                return udcs[0].name
        return None
    
    def _write_file(self, path: Path, content: str):
        """Write content to a file"""
        with open(path, 'w') as f:
            f.write(content)
    
    def _rmdir_recursive(self, path: Path):
        """Remove directory if it exists"""
        if path.exists() and path.is_dir():
            path.rmdir()


class HIDForwarder:
    """Forwards HID reports from internal devices to USB gadget"""
    
    def __init__(self, config: dict, no_usb: bool = False, hide_events: bool = False):
        self.config = config
        self.no_usb = no_usb
        self.hide_events = hide_events
        self.running = False
        self.grabbed = False
        
        self.keyboard_fd: Optional[int] = None
        self.mouse_fd: Optional[int] = None
        self.uinput_keyboard_fd: Optional[int] = None
        self.uinput_mouse_fd: Optional[int] = None
        self.hid_output_fd: Optional[int] = None
        
        self.gadget = USBGadget()
        
        # Setup signal handlers
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
    
    def _signal_handler(self, signum, frame):
        """Handle shutdown signals"""
        print("\nReceived signal, shutting down...")
        self.running = False
    
    def find_hidraw_device(self, device_type: str, vid: int, pid: int) -> Optional[int]:
        """Find hidraw device by VID/PID"""
        for i in range(16):
            path = f'/dev/hidraw{i}'
            try:
                fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
                
                # HIDIOCGRAWINFO ioctl
                HIDIOCGRAWINFO = 0x80084803
                buf = fcntl.ioctl(fd, HIDIOCGRAWINFO, bytes(12))
                
                # Unpack bustype, vendor, product
                bustype, vendor, product = struct.unpack('IHH', buf[:8])
                
                if vendor == vid and product == pid:
                    print(f"Found {device_type} at: {path}")
                    return fd
                
                os.close(fd)
            except (OSError, IOError):
                continue
        
        return None
    
    def grab_device(self, dev_path: str) -> Optional[int]:
        """Grab an input device"""
        print(f"Grabbing: {dev_path}")
        try:
            fd = os.open(dev_path, os.O_RDONLY)
            
            # Ungrab first
            try:
                fcntl.ioctl(fd, EVIOCGRAB, 0)
            except:
                pass
            
            time.sleep(0.5)
            
            # Attempt to grab device
            fcntl.ioctl(fd, EVIOCGRAB, 1)
            return fd
        except Exception as e:
            print(f"Error grabbing device {dev_path}: {e}")
            return None
    
    def ungrab_device(self, fd: int):
        """Ungrab an input device"""
        if fd is not None:
            try:
                fcntl.ioctl(fd, EVIOCGRAB, 0)
                os.close(fd)
            except:
                pass
    
    def grab_both(self):
        """Grab the keyboard and mouse devices"""
        print("Grabbing Keyboard and/or Mouse")
        
        if self.keyboard_fd is not None:
            self.uinput_keyboard_fd = self.grab_device(self.config['keyboard_dev'])
        
        if self.mouse_fd is not None:
            self.uinput_mouse_fd = self.grab_device(self.config['mouse_dev'])
        
        if self.uinput_keyboard_fd is not None or self.uinput_mouse_fd is not None:
            self.grabbed = True
    
    def ungrab_both(self):
        """Ungrab the keyboard and mouse devices"""
        print("Releasing Keyboard and/or Mouse")
        
        if self.uinput_keyboard_fd is not None:
            self.ungrab_device(self.uinput_keyboard_fd)
            self.uinput_keyboard_fd = None
        
        if self.uinput_mouse_fd is not None:
            self.ungrab_device(self.uinput_mouse_fd)
            self.uinput_mouse_fd = None
        
        self.grabbed = False
    
    def send_empty_reports(self):
        """Send empty HID reports to release all keys/buttons"""
        if self.no_usb or self.hid_output_fd is None:
            return
        
        if self.keyboard_fd is not None:
            report = bytes([1] + [0] * KEYBOARD_HID_REPORT_SIZE)
            try:
                os.write(self.hid_output_fd, report)
            except (OSError, BrokenPipeError) as e:
                print("Keyboard BrokenPipeError - likely USB isn't connected to destination PC")
                pass
        
        if self.mouse_fd is not None:
            report = bytes([2] + [0] * MOUSE_HID_REPORT_SIZE)
            try:
                os.write(self.hid_output_fd, report)
            except (OSError, BrokenPipeError) as e:
                print("Mouse BrokenPipeError - likely USB isn't connected to destination PC")
                pass
    
    def run(self) -> int:
        """Main forwarding loop"""
        # Find HID devices
        self.keyboard_fd = self.find_hidraw_device(
            'keyboard',
            self.config['keyboard_vid'],
            self.config['keyboard_pid']
        )
        if self.keyboard_fd is None:
            print("Failed to open keyboard device")
        
        self.mouse_fd = self.find_hidraw_device(
            'mouse',
            self.config['mouse_vid'],
            self.config['mouse_pid']
        )
        if self.mouse_fd is None:
            print("Failed to open mouse device")
        
        if self.keyboard_fd is None and self.mouse_fd is None:
            print("No devices to forward, quitting program")
            return 1
        
        # Initialize USB gadget
        if not self.no_usb:
            if not self.gadget.init(self.config['keyboard_vid'], self.config['keyboard_pid']):
                print("Failed to initialize USB gadget")
                return 1
            
            # Wait for hidg0 to appear and open it
            hidg_path = '/dev/hidg0'
            for _ in range(50):  # Try for 5 seconds
                try:
                    self.hid_output_fd = os.open(hidg_path, os.O_WRONLY | os.O_NONBLOCK)
                    break
                except OSError as e:
                    if e.errno == errno.EINTR:
                        continue
                    time.sleep(0.1)
            
            if self.hid_output_fd is None:
                print(f"Error opening {hidg_path} for writing")
                return 1
        
        # Grab devices
        self.grab_both()
        
        print("Running... (Ctrl+Raspberry to toggle capture, Ctrl+Shift+Raspberry to exit)")
        self.running = True
        
        # Main loop
        try:
            while self.running:
                # Read from keyboard
                if self.keyboard_fd is not None:
                    try:
                        data = os.read(self.keyboard_fd, KEYBOARD_HID_REPORT_SIZE)
                        if len(data) == KEYBOARD_HID_REPORT_SIZE:
                            if not self.hide_events:
                                print(f"K: {' '.join(f'{b:02x}' for b in data)}")
                            
                            # Forward to USB gadget if grabbed
                            if not self.no_usb and self.grabbed and self.hid_output_fd is not None:
                                report = bytes([1]) + data
                                try:
                                    os.write(self.hid_output_fd, report)
                                    time.sleep(0.001)
                                except (OSError, BrokenPipeError) as e:
                                    # Ignore pipe errors when no host is connected
                                    if hasattr(e, 'errno') and e.errno not in (EPIPE, ESHUTDOWN):
                                        print(f"Warning: Error writing keyboard report: {e}")
                            
                            # Check for special key combinations
                            if len(data) > 0:
                                # Ctrl + Raspberry (0x09) - toggle capture
                                if data[0] == 0x09:
                                    if self.grabbed:
                                        self.ungrab_both()
                                        self.send_empty_reports()
                                    else:
                                        self.grab_both()
                                
                                # Ctrl + Shift + Raspberry (0x0b) - exit
                                elif data[0] == 0x0b:
                                    self.running = False
                                    break
                    except OSError as e:
                        if e.errno != errno.EAGAIN:
                            raise
                
                # Read from mouse
                if self.mouse_fd is not None:
                    try:
                        data = os.read(self.mouse_fd, MOUSE_HID_REPORT_SIZE)
                        if len(data) == MOUSE_HID_REPORT_SIZE:
                            if not self.hide_events:
                                print(f"M: {' '.join(f'{b:02x}' for b in data)}")
                            
                            # Forward to USB gadget if grabbed
                            if not self.no_usb and self.grabbed and self.hid_output_fd is not None:
                                report = bytes([2]) + data
                                try:
                                    os.write(self.hid_output_fd, report)
                                    time.sleep(0.001)
                                except (OSError, BrokenPipeError) as e:
                                    # Ignore pipe errors when no host is connected
                                    if hasattr(e, 'errno') and e.errno not in (EPIPE, ESHUTDOWN):
                                        print(f"Warning: Error writing mouse report: {e}")
                    except OSError as e:
                        if e.errno != errno.EAGAIN:
                            raise
                
                # Small sleep to prevent busy waiting
                time.sleep(0.001)
        
        finally:
            # Cleanup
            self.ungrab_both()
            self.send_empty_reports()
            
            if self.keyboard_fd is not None:
                os.close(self.keyboard_fd)
            if self.mouse_fd is not None:
                os.close(self.mouse_fd)
            if self.hid_output_fd is not None:
                os.close(self.hid_output_fd)
            
            if not self.no_usb:
                print("Cleanup USB")
                self.gadget.cleanup()
        
        return 0


def main():
    parser = argparse.ArgumentParser(
        description='Pi USB HID Forwarder - Forward keyboard/mouse to USB gadget mode'
    )
    
    # Model presets
    model_group = parser.add_mutually_exclusive_group()
    model_group.add_argument('--pi400', action='store_true',
                            help='Use Pi 400 configuration')
    model_group.add_argument('--pi500', action='store_true',
                            help='Use Pi 500 configuration')
    model_group.add_argument('--pi500plus', action='store_true',
                            help='Use Pi 500+ configuration')
    
    # Manual overrides
    parser.add_argument('--keyboard-vid', type=lambda x: int(x, 0),
                       help='Keyboard vendor ID (hex or decimal)')
    parser.add_argument('--keyboard-pid', type=lambda x: int(x, 0),
                       help='Keyboard product ID (hex or decimal)')
    parser.add_argument('--keyboard-dev', type=str,
                       help='Keyboard device path')
    parser.add_argument('--mouse-vid', type=lambda x: int(x, 0),
                       help='Mouse vendor ID (hex or decimal)')
    parser.add_argument('--mouse-pid', type=lambda x: int(x, 0),
                       help='Mouse product ID (hex or decimal)')
    parser.add_argument('--mouse-dev', type=str,
                       help='Mouse device path')
   
    # Output options
    parser.add_argument('--no-usb', action='store_true',
                       help='Disable USB output (testing mode)')
    parser.add_argument('--hide-events', action='store_true',
                   help='Hide the keyboard and mouse event output on screen')
    
    args = parser.parse_args()
    
    # Determine configuration
    if args.pi500:
        config = DEVICE_CONFIGS['pi500'].copy()
        print("Using Pi 500 configuration")
    elif args.pi500plus:
        config = DEVICE_CONFIGS['pi500plus'].copy()
        print("Using Pi 500+ configuration")
    else:
        # Default to Pi 400
        config = DEVICE_CONFIGS['pi400'].copy()
        print("Using Pi 400 configuration")
    
    # Apply manual overrides
    if args.keyboard_vid is not None:
        config['keyboard_vid'] = args.keyboard_vid
    if args.keyboard_pid is not None:
        config['keyboard_pid'] = args.keyboard_pid
    if args.keyboard_dev is not None:
        config['keyboard_dev'] = args.keyboard_dev
    
    if args.mouse_vid is not None:
        config['mouse_vid'] = args.mouse_vid
    if args.mouse_pid is not None:
        config['mouse_pid'] = args.mouse_pid
    if args.mouse_dev is not None:
        config['mouse_dev'] = args.mouse_dev
    
    # Check for root permissions
    if os.geteuid() != 0:
        print("Error: This program must be run as root: sudo ./py400kb.py")
        return 1
    
    # Run forwarder
    forwarder = HIDForwarder(config, no_usb=args.no_usb, hide_events=args.hide_events)

    return forwarder.run()

if __name__ == '__main__':
    sys.exit(main())
