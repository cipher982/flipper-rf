#!/usr/bin/env python3
"""
Flipper Zero CLI automation tool for Claude Code integration.
Provides a clean interface for device exploration and automation.
"""

import serial
import time
import json
import re
import sys
from pathlib import Path
from typing import Optional, List, Dict, Any

class FlipperZero:
    """Flipper Zero serial CLI interface."""

    def __init__(self, port: str = '/dev/cu.usbmodemflip_Ly0p11', baud: int = 230400):
        self.port = port
        self.baud = baud
        self.ser: Optional[serial.Serial] = None

    def connect(self) -> bool:
        """Connect to Flipper Zero."""
        try:
            self.ser = serial.Serial(self.port, self.baud, timeout=3)
            time.sleep(0.3)
            self.ser.read(self.ser.in_waiting)  # Clear buffer
            return True
        except Exception as e:
            print(f"Connection error: {e}", file=sys.stderr)
            return False

    def disconnect(self):
        """Disconnect from Flipper Zero."""
        if self.ser:
            self.ser.close()
            self.ser = None

    def cmd(self, command: str, wait: float = 0.5, max_bytes: int = 16384) -> str:
        """Execute a CLI command and return response."""
        if not self.ser:
            raise RuntimeError("Not connected")
        self.ser.write(f'{command}\r\n'.encode())
        time.sleep(wait)
        response = self.ser.read(self.ser.in_waiting or max_bytes)
        return response.decode('utf-8', errors='replace')

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *args):
        self.disconnect()

    # ========== Device Info ==========

    def device_info(self) -> Dict[str, str]:
        """Get comprehensive device information."""
        raw = self.cmd('device_info', 1.5)
        info = {}
        for line in raw.split('\n'):
            if ':' in line and not line.strip().startswith('>'):
                parts = line.split(':', 1)
                if len(parts) == 2:
                    key = parts[0].strip()
                    val = parts[1].strip()
                    if key and not key.startswith('['):
                        info[key] = val
        return info

    def uptime(self) -> str:
        """Get device uptime."""
        raw = self.cmd('uptime')
        match = re.search(r'Uptime:\s*(.+)', raw)
        return match.group(1) if match else raw

    def free_memory(self) -> Dict[str, int]:
        """Get memory statistics."""
        raw = self.cmd('free')
        mem = {}
        patterns = {
            'free_heap': r'Free heap size:\s*(\d+)',
            'total_heap': r'Total heap size:\s*(\d+)',
            'min_heap': r'Minimum heap size:\s*(\d+)',
            'max_block': r'Maximum heap block:\s*(\d+)',
        }
        for key, pattern in patterns.items():
            match = re.search(pattern, raw)
            if match:
                mem[key] = int(match.group(1))
        return mem

    # ========== Storage ==========

    def storage_info(self, path: str = '/ext') -> Dict[str, Any]:
        """Get storage information."""
        raw = self.cmd(f'storage info {path}')
        info = {}
        for line in raw.split('\n'):
            if 'total' in line.lower():
                match = re.search(r'(\d+)KiB total', line)
                if match:
                    info['total_kb'] = int(match.group(1))
            if 'free' in line.lower():
                match = re.search(r'(\d+)KiB free', line)
                if match:
                    info['free_kb'] = int(match.group(1))
        return info

    def list_dir(self, path: str) -> List[Dict[str, Any]]:
        """List directory contents."""
        raw = self.cmd(f'storage list {path}', 1)
        items = []
        for line in raw.split('\n'):
            # [D] dirname or [F] filename sizeb
            match = re.search(r'\[([DF])\]\s+(\S+)(?:\s+(\d+)b)?', line)
            if match:
                item = {
                    'type': 'dir' if match.group(1) == 'D' else 'file',
                    'name': match.group(2),
                }
                if match.group(3):
                    item['size'] = int(match.group(3))
                items.append(item)
        return items

    def read_file(self, path: str) -> str:
        """Read a file from Flipper storage."""
        raw = self.cmd(f'storage read {path}', 1)
        # Strip command echo and prompt
        lines = raw.split('\n')
        content_lines = []
        for line in lines:
            if not line.startswith('>:') and 'storage read' not in line:
                content_lines.append(line)
        return '\n'.join(content_lines).strip()

    # ========== SubGHz ==========

    def subghz_rx(self, frequency: int = 433920000, duration: float = 5.0, device: int = 0) -> str:
        """Receive on SubGHz frequency for given duration."""
        # Start receiving
        self.ser.write(f'subghz rx {frequency} {device}\r\n'.encode())
        time.sleep(duration)
        # Send Ctrl+C to stop
        self.ser.write(b'\x03')
        time.sleep(0.3)
        return self.ser.read(self.ser.in_waiting or 16384).decode('utf-8', errors='replace')

    # ========== Infrared ==========

    def ir_rx(self, raw: bool = False, timeout: float = 10.0) -> str:
        """Receive IR signal."""
        cmd = 'ir rx raw' if raw else 'ir rx'
        self.ser.write(f'{cmd}\r\n'.encode())
        time.sleep(timeout)
        self.ser.write(b'\x03')  # Ctrl+C to stop
        time.sleep(0.3)
        return self.ser.read(self.ser.in_waiting or 16384).decode('utf-8', errors='replace')

    def ir_tx(self, protocol: str, address: str, command: str) -> str:
        """Transmit IR signal."""
        return self.cmd(f'ir tx {protocol} {address} {command}')

    def ir_universal(self, remote: str, signal: str) -> str:
        """Send universal IR command."""
        return self.cmd(f'ir universal {remote} {signal}')

    # ========== GPIO ==========

    def gpio_read(self, pin: str) -> Optional[int]:
        """Read GPIO pin value."""
        raw = self.cmd(f'gpio read {pin}')
        match = re.search(r'(\d+)', raw)
        return int(match.group(1)) if match else None

    def gpio_set(self, pin: str, value: int) -> str:
        """Set GPIO pin value (requires output mode)."""
        return self.cmd(f'gpio set {pin} {value}')

    def gpio_mode(self, pin: str, output: bool = False) -> str:
        """Set GPIO pin mode (0=input, 1=output)."""
        return self.cmd(f'gpio mode {pin} {1 if output else 0}')


def main():
    """CLI interface for testing."""
    import argparse
    parser = argparse.ArgumentParser(description='Flipper Zero CLI Tool')
    parser.add_argument('command', choices=['info', 'ls', 'read', 'ir-rx', 'subghz-rx'])
    parser.add_argument('--path', default='/ext', help='Storage path')
    parser.add_argument('--freq', type=int, default=433920000, help='Frequency in Hz')
    parser.add_argument('--duration', type=float, default=5.0, help='Duration in seconds')
    parser.add_argument('--json', action='store_true', help='Output as JSON')
    args = parser.parse_args()

    with FlipperZero() as f:
        if args.command == 'info':
            result = f.device_info()
        elif args.command == 'ls':
            result = f.list_dir(args.path)
        elif args.command == 'read':
            result = f.read_file(args.path)
        elif args.command == 'ir-rx':
            result = f.ir_rx(timeout=args.duration)
        elif args.command == 'subghz-rx':
            result = f.subghz_rx(args.freq, args.duration)

        if args.json and isinstance(result, (dict, list)):
            print(json.dumps(result, indent=2))
        else:
            print(result)


if __name__ == '__main__':
    main()
