"""ADB: enumerate attached devices, run commands, stream their output."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import threading
from dataclasses import dataclass
from typing import Callable

from .stream import LinePump

#: Keeps adb from flashing a console window on top of the GUI.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


class AdbError(RuntimeError):
    pass


@dataclass(frozen=True)
class AdbDevice:
    serial: str
    state: str
    model: str = ""
    product: str = ""
    device: str = ""
    transport_id: str = ""

    @property
    def is_ready(self) -> bool:
        return self.state == "device"

    @property
    def label(self) -> str:
        name = self.model or self.product or self.device
        suffix = f"  ({name})" if name else ""
        return f"{self.serial}  [{self.state}]{suffix}"


def resolve_adb(path: str = "adb") -> str:
    """Turn a bare name into a full path so the GUI can report what it found."""
    return shutil.which(path or "adb") or (path or "adb")


def parse_devices(text: str) -> list[AdbDevice]:
    """Parse the output of ``adb devices -l``."""
    devices: list[AdbDevice] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices") or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        extra = {}
        for token in parts[2:]:
            if ":" in token:
                key, value = token.split(":", 1)
                extra[key] = value
        devices.append(
            AdbDevice(
                serial=parts[0],
                state=parts[1],
                model=extra.get("model", ""),
                product=extra.get("product", ""),
                device=extra.get("device", ""),
                transport_id=extra.get("transport_id", ""),
            )
        )
    return devices


class Adb:
    """An adb client, optionally bound to one device.

    A client is safe to use from a background thread; never call it from the GUI
    thread, since every method here blocks on a subprocess.
    """

    def __init__(self, adb_path: str = "adb", serial: str = "") -> None:
        self.adb_path = resolve_adb(adb_path)
        self.serial = serial
        self._running: list[tuple[subprocess.Popen, LinePump]] = []
        self._running_lock = threading.Lock()

    def list_devices(self, timeout: float = 10.0) -> list[AdbDevice]:
        return parse_devices(self._exec(["devices", "-l"], timeout=timeout))

    def _argv(self, args: list[str]) -> list[str]:
        argv = [self.adb_path]
        if self.serial:
            argv += ["-s", self.serial]
        return argv + args

    def _exec(self, args: list[str], timeout: float) -> str:
        argv = self._argv(args)
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=timeout,
                creationflags=_NO_WINDOW,
            )
        except FileNotFoundError as exc:
            raise AdbError(f"找不到 adb：{self.adb_path}") from exc
        except subprocess.TimeoutExpired as exc:
            raise AdbError(f"adb 命令超时（{timeout}s）：{' '.join(argv)}") from exc
        output = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode != 0:
            raise AdbError(output.strip() or f"adb 退出码 {proc.returncode}")
        return output

    def run(self, command: str | list[str], timeout: float = 30.0) -> str:
        """Run a command to completion and return its combined output.

        A string is split with shell quoting rules, so ``shell "echo a b"``
        works as written. Pass a list when you want the argument boundaries to
        be taken literally.
        """
        args = shlex.split(command) if isinstance(command, str) else list(command)
        if not args:
            raise AdbError("空命令")
        return self._exec(args, timeout=timeout)

    def stream(
        self,
        command: str | list[str],
        on_line: Callable[[str], None],
        on_exit: Callable[[int], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
    ) -> LinePump:
        """Start a long-running command and deliver its output as it arrives.

        Used for logcat and anything else that never returns on its own. Call
        ``stop_all`` when tearing the bench down.
        """
        args = shlex.split(command) if isinstance(command, str) else list(command)
        argv = self._argv(args)
        try:
            proc = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                creationflags=_NO_WINDOW,
            )
        except FileNotFoundError as exc:
            raise AdbError(f"找不到 adb：{self.adb_path}") from exc

        pump = LinePump(
            proc.stdout, on_line, name=f"adb-{self.serial or 'default'}", on_error=on_error
        )
        with self._running_lock:
            self._running.append((proc, pump))
        pump.start()

        if on_exit is not None:
            threading.Thread(
                target=self._watch, args=(proc, on_exit), daemon=True
            ).start()
        return pump

    @staticmethod
    def _watch(proc: subprocess.Popen, on_exit: Callable[[int], None]) -> None:
        code = proc.wait()
        on_exit(code)

    def stop_all(self) -> None:
        """Terminate every streamed command started through this client."""
        with self._running_lock:
            running, self._running = self._running, []
        for proc, pump in running:
            pump.stop()
            if proc.poll() is None:
                proc.terminate()
