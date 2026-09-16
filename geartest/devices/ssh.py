"""SSH to a build/test host, optionally entering a container on it.

The bench uses this to drive commands inside a dev container: connect to the
host, ``cd`` to the working directory, optionally ``docker exec`` into a
container, then run the command. Output can be streamed, and files moved over
SFTP.
"""

from __future__ import annotations

import shlex
import threading
from typing import Callable

import paramiko

from ..config import SshCfg
from .stream import LinePump


class SshError(RuntimeError):
    pass


class SshHost:
    """One SSH connection, optionally wrapped in ``docker exec``."""

    def __init__(self, cfg: SshCfg, on_error: Callable[[Exception], None] | None = None) -> None:
        self.cfg = cfg
        self._on_error = on_error
        self._client: paramiko.SSHClient | None = None
        self._sftp: paramiko.SFTPClient | None = None
        self._pumps: list[LinePump] = []
        self._lock = threading.Lock()

    @property
    def is_open(self) -> bool:
        client = self._client
        if client is None:
            return False
        transport = client.get_transport()
        return transport is not None and transport.is_active()

    # -- connection ---------------------------------------------------------

    def open(self) -> None:
        if self.is_open:
            return
        c = self.cfg
        if not c.host:
            raise SshError("未配置 SSH 主机")

        client = paramiko.SSHClient()
        client.load_system_host_keys()
        # A bench host is configured by hand and reached by name on a lab
        # network. Accepting an unseen key keeps the first connection from
        # failing on a prompt nobody is there to answer; add the host to
        # known_hosts if you want it pinned.
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                hostname=c.host,
                port=c.port,
                username=c.username,
                password=c.password or None,
                key_filename=c.key_file or None,
                timeout=10.0,
                allow_agent=True,
                look_for_keys=not c.password,
            )
        except Exception as exc:
            client.close()
            raise SshError(f"连接 {c.username}@{c.host}:{c.port} 失败：{exc}") from exc
        self._client = client

    def close(self) -> None:
        for pump in self._pumps:
            pump.stop()
        self._pumps.clear()
        sftp, self._sftp = self._sftp, None
        if sftp is not None:
            try:
                sftp.close()
            except Exception:
                pass
        client, self._client = self._client, None
        if client is not None:
            client.close()

    def _require(self) -> paramiko.SSHClient:
        if not self.is_open:
            self.open()
        client = self._client
        if client is None:
            raise SshError("SSH 连接不可用")
        return client

    # -- command construction ----------------------------------------------

    def wrap(self, command: str) -> str:
        """Build the remote command line.

        Order is working directory, then container, then the command. To run
        inside a container a shell has to be named explicitly, so set
        ``docker_exec`` to the full prefix, e.g.
        ``docker exec -i mycontainer sh -c``.
        """
        inner = command
        if self.cfg.docker_exec:
            inner = f"{self.cfg.docker_exec} {shlex.quote(inner)}"
        if self.cfg.workdir:
            inner = f"cd {shlex.quote(self.cfg.workdir)} && {inner}"
        return inner

    # -- running commands ---------------------------------------------------

    def run(self, command: str, timeout: float = 60.0) -> str:
        """Run a command to completion and return its stdout."""
        client = self._require()
        _, stdout, stderr = client.exec_command(self.wrap(command), timeout=timeout)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        code = stdout.channel.recv_exit_status()
        if code != 0:
            raise SshError(err.strip() or out.strip() or f"命令退出码 {code}")
        return out

    def stream(
        self,
        command: str,
        on_line: Callable[[str], None],
        on_exit: Callable[[int], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
    ) -> LinePump:
        """Run a command and deliver its output as it arrives."""
        client = self._require()
        transport = client.get_transport()
        if transport is None:
            raise SshError("SSH 传输层已断开")

        channel = transport.open_session()
        channel.set_combine_stderr(True)
        channel.exec_command(self.wrap(command))

        pump = LinePump(
            channel.makefile("r", encoding="utf-8", errors="replace"),
            on_line,
            name=f"ssh-{self.cfg.name}",
            on_error=on_error or self._on_error,
        )
        self._pumps.append(pump)
        pump.start()

        if on_exit is not None:
            threading.Thread(
                target=self._wait_exit, args=(channel, on_exit), daemon=True
            ).start()
        return pump

    @staticmethod
    def _wait_exit(channel: paramiko.Channel, on_exit: Callable[[int], None]) -> None:
        on_exit(channel.recv_exit_status())

    # -- files --------------------------------------------------------------

    def _require_sftp(self) -> paramiko.SFTPClient:
        if self._sftp is None:
            self._sftp = self._require().open_sftp()
        return self._sftp

    def upload(self, local_path: str, remote_path: str) -> None:
        self._require_sftp().put(local_path, remote_path)

    def download(self, remote_path: str, local_path: str) -> None:
        self._require_sftp().get(remote_path, local_path)

    def listdir(self, remote_path: str) -> list[str]:
        return self._require_sftp().listdir(remote_path)

    def remote_join(self, *parts: str) -> str:
        """Join paths with forward slashes, which is what the remote end wants."""
        return "/".join(p.strip("/") for p in parts if p)
