#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import socket
import struct
import subprocess
import tempfile
import time
from pathlib import Path


SCREEN_COLUMNS = 40
SCREEN_ROWS = 24
BYTES_PER_PIXEL = 4
MAX_DISTANCE = 0.20
XK_SUPER_L = 0xFFEB
XK_L = ord("l")


class VncClient:
    def __init__(self, sock: socket.socket, width: int, height: int):
        self.sock = sock
        self.width = width
        self.height = height
        self.framebuffer = bytearray(width * height * BYTES_PER_PIXEL)

    @staticmethod
    def _read_exact(sock: socket.socket, size: int) -> bytes:
        chunks: list[bytes] = []
        remaining = size
        while remaining:
            chunk = sock.recv(remaining)
            if not chunk:
                raise RuntimeError("VNC server closed the connection")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    @classmethod
    def connect(cls, host: str, port: int, timeout: float) -> "VncClient":
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.settimeout(15.0)

        server_version = cls._read_exact(sock, 12)
        if not server_version.startswith(b"RFB "):
            raise RuntimeError(f"Unexpected VNC protocol header: {server_version!r}")

        sock.sendall(b"RFB 003.008\n")
        security_count = cls._read_exact(sock, 1)[0]
        security_types = cls._read_exact(sock, security_count)
        if 1 not in security_types:
            raise RuntimeError(
                f"VNC server does not offer unauthenticated access: {security_types!r}"
            )

        sock.sendall(b"\x01")
        if cls._read_exact(sock, 4) != b"\x00\x00\x00\x00":
            raise RuntimeError("VNC security negotiation failed")

        sock.sendall(b"\x01")
        width, height = struct.unpack(">HH", cls._read_exact(sock, 4))
        cls._read_exact(sock, 16)
        name_length = struct.unpack(">I", cls._read_exact(sock, 4))[0]
        cls._read_exact(sock, name_length)

        pixel_format = struct.pack(
            ">BBBBHHHBBB3x",
            32,
            24,
            0,
            1,
            255,
            255,
            255,
            16,
            8,
            0,
        )
        sock.sendall(b"\x00\x00\x00\x00" + pixel_format)
        sock.sendall(struct.pack(">BBHi", 2, 0, 1, 0))

        return cls(sock, width, height)

    def close(self) -> None:
        self.sock.close()

    def _request_full_frame(self) -> None:
        self.sock.sendall(
            struct.pack(">BBHHHH", 3, 0, 0, 0, self.width, self.height)
        )

    def _key_event(self, keysym: int, pressed: bool) -> None:
        self.sock.sendall(struct.pack(">BBHI", 4, int(pressed), 0, keysym))

    def key_chord(self, keys: list[int]) -> None:
        for keysym in keys:
            self._key_event(keysym, True)
        for keysym in reversed(keys):
            self._key_event(keysym, False)

    def _receive_frame(self) -> None:
        updated_area = 0
        expected_area = self.width * self.height
        deadline = time.monotonic() + 15.0

        while updated_area < expected_area:
            if time.monotonic() >= deadline:
                raise RuntimeError("Timed out waiting for a complete VNC framebuffer")

            message_type = self._read_exact(self.sock, 1)[0]

            if message_type == 0:
                self._read_exact(self.sock, 1)
                rectangle_count = struct.unpack(">H", self._read_exact(self.sock, 2))[0]

                for _ in range(rectangle_count):
                    header = self._read_exact(self.sock, 12)
                    x, y, width, height, encoding = struct.unpack(">HHHHI", header)

                    if encoding != 0:
                        raise RuntimeError(
                            f"VNC server returned unsupported encoding {encoding}"
                        )

                    if x + width > self.width or y + height > self.height:
                        raise RuntimeError("VNC rectangle exceeds the framebuffer")

                    raw = self._read_exact(
                        self.sock,
                        width * height * BYTES_PER_PIXEL
                    )
                    row_size = width * BYTES_PER_PIXEL
                    for row in range(height):
                        source_start = row * row_size
                        target_start = (
                            (y + row) * self.width + x
                        ) * BYTES_PER_PIXEL
                        self.framebuffer[target_start : target_start + row_size] = raw[
                            source_start : source_start + row_size
                        ]

                    updated_area += width * height
            elif message_type == 2:
                continue
            elif message_type == 3:
                self._read_exact(self.sock, 3)
                length = struct.unpack(">I", self._read_exact(self.sock, 4))[0]
                self._read_exact(self.sock, length)
            else:
                raise RuntimeError(
                    f"VNC server returned unsupported message type {message_type}"
                )

    def capture(self) -> bytes:
        self._request_full_frame()
        self._receive_frame()
        samples = bytearray()

        for row in range(SCREEN_ROWS):
            y = min(
                self.height - 1,
                int((row + 0.5) * self.height / SCREEN_ROWS),
            )
            for column in range(SCREEN_COLUMNS):
                x = min(
                    self.width - 1,
                    int((column + 0.5) * self.width / SCREEN_COLUMNS),
                )
                offset = (y * self.width + x) * BYTES_PER_PIXEL
                pixel = self.framebuffer[offset : offset + 3]
                samples.extend(channel & 0xF0 for channel in pixel)

        return bytes(samples)

    @staticmethod
    def distance(first: bytes, second: bytes) -> float:
        if len(first) != len(second):
            return 1.0

        different = sum(
            abs(left - right) > 32
            for left, right in zip(first, second)
        )
        return different / len(first)

    @staticmethod
    def looks_blank(signature: bytes) -> bool:
        pixels = {
            signature[index : index + 3]
            for index in range(0, len(signature), 3)
        }
        return len(pixels) < 12

    def wait_for_stable_desktop(
        self,
        boot_timeout: float,
        minimum_boot_seconds: float,
    ) -> bytes:
        deadline = time.monotonic() + boot_timeout
        started = time.monotonic()
        previous: bytes | None = None
        stable_samples = 0

        while time.monotonic() < deadline:
            current = self.capture()
            elapsed = time.monotonic() - started

            if (
                elapsed >= minimum_boot_seconds
                and not self.looks_blank(current)
                and previous is not None
            ):
                if self.distance(previous, current) < 0.08:
                    stable_samples += 1
                else:
                    stable_samples = 0

                if stable_samples >= 3:
                    return current

            previous = current
            time.sleep(3)

        raise RuntimeError("Graphical live session did not reach a stable framebuffer")


def start_qemu(iso: Path, disk: Path, log_path: Path) -> subprocess.Popen[bytes]:
    qemu = shutil.which("qemu-system-x86_64")
    if qemu is None:
        raise RuntimeError("qemu-system-x86_64 is not installed")

    disk_handle = disk.open("wb")
    disk_handle.truncate(8 * 1024 * 1024 * 1024)
    disk_handle.close()

    command = [
        qemu,
        "-name",
        "TerminalOS-vm-smoke",
        "-machine",
        "pc,accel=tcg",
        "-cpu",
        "qemu64",
        "-smp",
        "4",
        "-m",
        "4096",
        "-drive",
        f"file={disk},if=virtio,format=raw",
        "-cdrom",
        str(iso),
        "-boot",
        "order=d",
        "-device",
        "virtio-vga",
        "-vnc",
        "127.0.0.1:1",
        "-display",
        "none",
        "-nic",
        "none",
        "-no-reboot",
    ]
    log_handle = log_path.open("wb")
    process = subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=log_handle,
    )
    process._terminalos_log_handle = log_handle  # type: ignore[attr-defined]
    return process


def connect_with_retry(
    host: str,
    port: int,
    timeout: float,
) -> VncClient:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None

    while time.monotonic() < deadline:
        try:
            return VncClient.connect(host, port, timeout=5.0)
        except (OSError, RuntimeError) as error:
            last_error = error
            time.sleep(2)

    raise RuntimeError(f"Could not connect to QEMU VNC: {last_error}")


def stop_qemu(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=15)

    log_handle = getattr(process, "_terminalos_log_handle", None)
    if log_handle is not None:
        log_handle.close()


def run_test(iso: Path, boot_timeout: float, idle_seconds: float) -> None:
    with tempfile.TemporaryDirectory(prefix="terminalos-vm-") as temporary:
        directory = Path(temporary)
        disk = directory / "disk.raw"
        log_path = directory / "qemu.log"
        process = start_qemu(iso, disk, log_path)

        client: VncClient | None = None
        try:
            client = connect_with_retry("127.0.0.1", 5901, boot_timeout)
            baseline = client.wait_for_stable_desktop(
                boot_timeout,
                minimum_boot_seconds=60.0,
            )

            time.sleep(idle_seconds)
            idle_signature = client.capture()
            idle_distance = client.distance(baseline, idle_signature)
            if idle_distance > MAX_DISTANCE:
                raise RuntimeError(
                    "Live session changed to a lock or blank screen while idle "
                    f"(visual distance {idle_distance:.3f})"
                )

            client.key_chord([XK_SUPER_L, XK_L])
            time.sleep(5)
            after_lock_request = client.capture()
            lock_distance = client.distance(baseline, after_lock_request)
            if lock_distance > MAX_DISTANCE:
                raise RuntimeError(
                    "Super+L produced a lock screen in the live session "
                    f"(visual distance {lock_distance:.3f})"
                )

            print(
                "VM smoke test passed: graphical live session booted, stayed "
                f"unlocked for {idle_seconds:.0f}s, and ignored Super+L."
            )
        finally:
            if client is not None:
                client.close()
            stop_qemu(process)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Boot a TerminalOS ISO in QEMU and verify the live session stays unlocked"
    )
    parser.add_argument("--iso", type=Path, required=True)
    parser.add_argument("--boot-timeout", type=float, default=360.0)
    parser.add_argument("--idle-seconds", type=float, default=300.0)
    return parser.parse_args()


def main() -> int:
    arguments = parse_args()
    iso = arguments.iso.resolve()

    if not iso.is_file():
        raise SystemExit(f"ISO does not exist: {iso}")

    try:
        run_test(iso, arguments.boot_timeout, arguments.idle_seconds)
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"VM smoke test failed: {error}", flush=True)
        raise SystemExit(1) from error

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
