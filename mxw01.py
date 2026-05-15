#!/usr/bin/env python3
"""
MXW01 Thermal Printer Protocol
Protocol implementation for MXW01 series printers (FunPrint compatible).
Based on https://github.com/PinThePenguinOne/MXW01_Thermal-Printer-Tool
"""

import asyncio
from typing import Callable, Optional

# GATT Characteristics
CONTROL_WRITE_UUID = "0000ae01-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID = "0000ae02-0000-1000-8000-00805f9b34fb"
DATA_WRITE_UUID = "0000ae03-0000-1000-8000-00805f9b34fb"

PRINTER_WIDTH_PIXELS = 384
PRINTER_WIDTH_BYTES = PRINTER_WIDTH_PIXELS // 8
MAX_CHUNK_HEIGHT = 256
WRITE_CHUNK_SIZE = 20


def _crc8(data: bytes) -> int:
    """CRC-8 with polynomial 0x07."""
    crc = 0x00
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x80:
                crc = (crc << 1) ^ 0x07
            else:
                crc <<= 1
            crc &= 0xFF
    return crc


def _cmd_with_crc(command_id: int, data: bytes) -> bytes:
    """Create command packet with CRC."""
    length = len(data).to_bytes(2, byteorder='little')
    crc = _crc8(data)
    return bytes([0x22, 0x21, command_id, 0x00]) + length + data + bytes([crc, 0xFF])


def _cmd_simple(command_id: int, data: bytes) -> bytes:
    """Create command packet without CRC."""
    length = len(data).to_bytes(2, byteorder='little')
    return bytes([0x22, 0x21, command_id, 0x00]) + length + data + bytes([0x00, 0x00])


def _parse_response(data: bytes):
    """Parse notification response. Returns (command_id, payload) or (None, None)."""
    if not data or len(data) < 8 or data[0] != 0x22 or data[1] != 0x21:
        return None, None
    command_id = data[2]
    payload_len = int.from_bytes(data[4:6], 'little')
    if len(data) < 6 + payload_len:
        return command_id, None
    payload = data[6:6 + payload_len]
    return command_id, payload


class MXW01Printer:
    """Protocol handler for MXW01 series thermal printers."""

    def __init__(self, client, msg: Optional[Callable[[str], None]] = None):
        self.client = client
        self._msg = msg or (lambda m: None)
        self._responses = {}
        self._condition = asyncio.Condition()
        self._ae01 = None
        self._ae02 = None
        self._ae03 = None

    async def setup(self):
        """Find characteristics and start notifications."""
        services = self.client.services
        self._ae01 = services.get_characteristic(CONTROL_WRITE_UUID)
        self._ae02 = services.get_characteristic(NOTIFY_UUID)
        self._ae03 = services.get_characteristic(DATA_WRITE_UUID)

        if not all([self._ae01, self._ae02, self._ae03]):
            raise RuntimeError("MXW01: Missing required GATT characteristics (AE01/AE02/AE03)")

        await self.client.start_notify(NOTIFY_UUID, self._on_notify)
        self._msg("MXW01: Notifications enabled")

    async def teardown(self):
        """Stop notifications."""
        try:
            await self.client.stop_notify(NOTIFY_UUID)
        except Exception:
            pass

    async def _on_notify(self, sender, data):
        """Handle incoming BLE notifications."""
        cmd_id, payload = _parse_response(data)
        if cmd_id is not None:
            async with self._condition:
                self._responses[cmd_id] = payload
                self._condition.notify_all()

    async def _wait_response(self, cmd_id: int, timeout: float = 7.0):
        """Wait for a specific command response."""
        async with self._condition:
            await asyncio.wait_for(
                self._condition.wait_for(lambda: cmd_id in self._responses),
                timeout=timeout
            )
            return self._responses.pop(cmd_id)

    async def _send_control(self, cmd: bytes):
        """Send command to control characteristic."""
        await self.client.write_gatt_char(self._ae01, cmd, response=False)
        await asyncio.sleep(0.01)

    async def _send_data(self, data: bytes):
        """Send data to data characteristic in chunks."""
        for i in range(0, len(data), WRITE_CHUNK_SIZE):
            chunk = data[i:i + WRITE_CHUNK_SIZE]
            await self.client.write_gatt_char(self._ae03, chunk, response=False)
            await asyncio.sleep(0.005)

    async def print_bitmap(self, bitmap_data: bytes, height: int):
        """Print bitmap data. Handles chunking for images taller than 256 lines."""
        lines_remaining = height
        offset = 0

        while lines_remaining > 0:
            chunk_height = min(lines_remaining, MAX_CHUNK_HEIGHT)
            chunk_bytes = chunk_height * PRINTER_WIDTH_BYTES
            chunk_data = bitmap_data[offset:offset + chunk_bytes]

            await self._print_chunk(chunk_data, chunk_height)

            offset += chunk_bytes
            lines_remaining -= chunk_height

            if lines_remaining > 0:
                self._msg(f"MXW01: {lines_remaining} lines remaining")
                await asyncio.sleep(1.0)

    async def _print_chunk(self, data: bytes, height: int):
        """Send one print chunk (up to 256 lines) through the full command sequence."""
        # 1. Setup: B1 → A2 → A1, wait for A1 response
        async with self._condition:
            self._responses.pop(0xA1, None)

        await self._send_control(_cmd_with_crc(0xB1, bytes([0x00])))
        await self._send_control(_cmd_with_crc(0xA2, bytes([0x5D])))
        await self._send_control(_cmd_with_crc(0xA1, bytes([0x00])))

        payload = await self._wait_response(0xA1)
        if not payload or len(payload) < 7:
            raise RuntimeError("MXW01: No A1 response from printer")
        status = payload[6] if len(payload) > 6 else 0xFF
        if status != 0:
            self._msg(f"MXW01: A1 status={status} (payload={payload.hex()})")
            raise RuntimeError(f"MXW01: Printer not ready (status={status}, may need charging or paper)")
        self._msg("MXW01: Printer ready")

        # 2. Print request: A2 → A9, wait for A9 response
        async with self._condition:
            self._responses.pop(0xA9, None)

        height_le = height.to_bytes(2, 'little')
        width_le = PRINTER_WIDTH_BYTES.to_bytes(2, 'little')

        await self._send_control(_cmd_with_crc(0xA2, bytes([0x5D])))
        await self._send_control(_cmd_simple(0xA9, height_le + width_le))

        payload = await self._wait_response(0xA9)
        if not payload or len(payload) < 1 or payload[0] != 0:
            raise RuntimeError("MXW01: Print request rejected (A9)")
        self._msg(f"MXW01: Printing {height} lines")

        # 3. Send image data via AE03
        await self._send_data(data)
        self._msg("MXW01: Data sent")

        # 4. End print: AD, wait for AA (print complete)
        async with self._condition:
            self._responses.pop(0xAA, None)

        await self._send_control(_cmd_simple(0xAD, bytes([0x00])))

        timeout = max(15.0, height / 20.0)
        try:
            await self._wait_response(0xAA, timeout=timeout)
            self._msg("MXW01: Print complete")
        except asyncio.TimeoutError:
            self._msg("MXW01: Warning - print complete signal not received (may still be printing)")

        await asyncio.sleep(0.5)

    async def feed(self, lines: int):
        """Feed blank paper."""
        blank_data = bytes([0x00] * (PRINTER_WIDTH_BYTES * lines))
        await self.print_bitmap(blank_data, lines)
