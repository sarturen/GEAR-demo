"""Sequential test flows described in YAML, bound to a device under test.

A flow names one device; its steps then refer to that device's resources by bare
name. Which board a rail is on, which COM port a console uses, which camera
watches which panel -- none of that appears in a flow, so retargeting a case at
another bench is a one-line change::

    name: KL30 上电点亮检查
    device: box1
    on_failure:
      - snapshot: {screens: all}
      - adb_logs: {save_to: logs}
    steps:
      - relay: {resource: KL30, action: "on"}
      - adb_wait: {timeout: 60}
      - expect_screen: {screen: 屏幕2, state: lit, timeout: 20}

Runs on whatever thread calls ``run``; call it from a worker thread, never from
the GUI thread. Progress arrives through the callbacks given to the constructor.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from ..bench import Bench, BenchError
from ..config import EVIDENCE_DIR, DeviceCfg
from ..devices.adb import AdbError
from ..devices.relay_modbus import RelayBoard
from ..devices.serial_link import LineCollector
from ..vision.detector import EventType

#: Events that mean "the screen was not showing anything".
DARK_TYPES = (EventType.BLACK_SCREEN, EventType.TRANSIENT_BLACKOUT)

SCREEN_WORDS = {
    "black": "黑屏",
    "dark": "变黑",
    "lit": "亮屏",
    "freeze": "冻屏",
    "no_freeze": "无冻屏",
    "no_black": "无黑屏",
}


class FlowError(RuntimeError):
    """The flow itself is malformed, as opposed to a step failing."""


class StepFailure(Exception):
    """A step's expectation was not met."""


@dataclass
class StepResult:
    index: int
    kind: str
    label: str
    ok: bool
    message: str
    seconds: float


@dataclass
class FlowResult:
    name: str
    device: str = ""
    results: list[StepResult] = field(default_factory=list)
    aborted: bool = False
    incident_dir: Path | None = None

    @property
    def ok(self) -> bool:
        return not self.aborted and all(r.ok for r in self.results)

    @property
    def summary(self) -> str:
        passed = sum(1 for r in self.results if r.ok)
        state = "通过" if self.ok else ("中止" if self.aborted else "失败")
        text = f"{self.name}：{state}（{passed}/{len(self.results)} 步通过）"
        if self.incident_dir is not None:
            text += f"　证据：{self.incident_dir}"
        return text


@dataclass
class Hit:
    at: float
    """Monotonic, for relating a hit to a power cycle."""
    wall: float
    """Wall clock, for finding it in a log."""
    line: str


class Watcher:
    """Watches one console for a pattern, in the background, for the whole run.

    A per-iteration assertion cannot do this job: a reset banner typically
    arrives a second or two after the rail returns, outside any tight window,
    and a polled check only looks while its own step is running.
    """

    def __init__(self, watch_id: str, console: str, pattern: re.Pattern) -> None:
        self.id = watch_id
        self.console = console
        self.pattern = pattern
        self.hits: list[Hit] = []
        self._lock = threading.Lock()

    def __call__(self, line: str) -> None:
        if self.pattern.search(line):
            with self._lock:
                self.hits.append(Hit(time.monotonic(), time.time(), line))

    def count(self) -> int:
        with self._lock:
            return len(self.hits)

    def first(self) -> Hit | None:
        with self._lock:
            return self.hits[0] if self.hits else None


def load_flow(path: str | Path) -> dict:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise FlowError(f"{path} 的内容不是一个映射")
    if "steps" not in data:
        raise FlowError(f"{path} 缺少 steps")
    return data


def _sleep(seconds: float, cancel: threading.Event, interval: float = 0.05) -> None:
    deadline = time.monotonic() + seconds
    while True:
        if cancel.is_set():
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(interval, remaining))


def _build_pattern(spec: dict) -> re.Pattern | None:
    """``contains`` matches literally; ``regex`` and its alias ``expect`` do not."""
    for key in ("contains", "regex", "expect"):
        value = spec.get(key)
        if value is None:
            continue
        return re.compile(re.escape(str(value)) if key == "contains" else str(value))
    return None


def _action(spec: dict, default: str = "on") -> str:
    """Read an action name, tolerating YAML's boolean spelling.

    In YAML 1.1 an unquoted ``on`` parses as the boolean True, not the string
    "on". Every operator writing ``action: on`` will hit this, so accept both.
    """
    value = spec.get("action", default)
    if value is True:
        return "on"
    if value is False:
        return "off"
    return str(value)


class FlowEngine:
    def __init__(
        self,
        bench: Bench,
        on_log: Callable[[str, str], None] | None = None,
        on_step: Callable[[StepResult], None] | None = None,
        on_screen_event: Callable[[Any], None] | None = None,
    ) -> None:
        self.bench = bench
        self._on_log = on_log
        self._on_step = on_step
        self._on_screen_event = on_screen_event
        self._cancel = threading.Event()
        self._collectors: dict[str, LineCollector] = {}
        self._watches: dict[str, Watcher] = {}
        self._device: DeviceCfg | None = None
        self._mark = 0.0
        self._iteration_mark = 0.0
        self._flow_start = 0.0
        self._named_marks: dict[str, float] = {}
        self._flow_name = ""
        self._incident: Path | None = None
        self._handlers = {
            "log": self._step_log,
            "mark": self._step_mark,
            "wait": self._step_wait,
            "relay": self._step_relay,
            "serial_write": self._step_serial_write,
            "serial_expect": self._step_serial_expect,
            "watch": self._step_watch,
            "expect_watch": self._step_expect_watch,
            "adb": self._step_adb,
            "adb_wait": self._step_adb_wait,
            "ssh": self._step_ssh,
            "expect_screen": self._step_expect_screen,
            "repeat": self._step_repeat,
            "snapshot": self._step_snapshot,
            "adb_logs": self._step_adb_logs,
        }

    # -- control ------------------------------------------------------------

    def cancel(self) -> None:
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def log(self, level: str, text: str) -> None:
        if self._on_log is not None:
            self._on_log(level, text)

    def describe(self, flow: dict) -> list[str]:
        """Labels for a flow's steps, so a run can be previewed before starting."""
        labels = []
        for index, step in enumerate(flow.get("steps") or []):
            try:
                kind, spec, _ = self._parse(step, index)
            except FlowError as exc:
                labels.append(str(exc))
                continue
            if kind not in self._handlers:
                labels.append(f"未知步骤类型 {kind}")
                continue
            labels.append(self._label(kind, spec))
        return labels

    # -- execution ----------------------------------------------------------

    def run(self, flow: dict, cancel: threading.Event | None = None) -> FlowResult:
        steps = flow.get("steps")
        if not isinstance(steps, list) or not steps:
            raise FlowError("steps 必须是非空列表")

        device_key = flow.get("device")
        if not device_key:
            raise FlowError(
                "流程必须声明它针对哪台设备，例如 device: box1 或 device: \"0123456789\""
            )
        self._device = self.bench.device(str(device_key))

        declared = self.bench.resolve_all(self._device.name)
        if declared:
            raise FlowError(
                f"设备「{self._device.name}」的资源声明有问题，未开始运行：\n"
                + "\n".join(f"  · {p}" for p in declared)
            )

        self._cancel = cancel if cancel is not None else threading.Event()
        self._flow_start = time.monotonic()
        self._mark = self._flow_start
        self._iteration_mark = self._flow_start
        self._flow_name = str(flow.get("name", "(未命名流程)"))
        self._incident = None
        self._watches.clear()
        for collector in self._collectors.values():
            collector.clear()

        result = FlowResult(name=self._flow_name, device=self._device.name)
        stop_on_failure = bool(flow.get("stop_on_failure", True))
        on_failure = flow.get("on_failure") or []

        self._start_device_screens()

        for index, step in enumerate(steps):
            if self._cancel.is_set():
                result.aborted = True
                break

            kind, spec, step_failure = self._parse(step, index)
            label = self._label(kind, spec)
            self.log("info", f"[{index + 1}/{len(steps)}] {label}")

            started = time.monotonic()
            ok, message = self._invoke(kind, spec, label)
            outcome = StepResult(
                index=index,
                kind=kind,
                label=label,
                ok=ok,
                message=message,
                seconds=time.monotonic() - started,
            )
            result.results.append(outcome)
            self._mark = time.monotonic()
            if self._on_step is not None:
                self._on_step(outcome)
            self.log(
                "info" if ok else "error",
                f"    {'PASS' if ok else 'FAIL'} — {message}（{outcome.seconds:.2f}s）",
            )

            if ok:
                continue

            # A step's own handlers run first, then the flow's.
            handlers = list(step_failure or []) + list(on_failure)
            if handlers:
                self._run_failure_handlers(handlers, outcome)

            if stop_on_failure:
                result.aborted = True
                break

        result.incident_dir = self._incident
        self.log("info", result.summary)
        return result

    def _invoke(self, kind: str, spec: Any, label: str) -> tuple[bool, str]:
        try:
            return True, (self._handlers[kind](spec) or "完成")
        except StepFailure as exc:
            return False, str(exc)
        except BenchError as exc:
            return False, str(exc)
        except FlowError as exc:
            return False, str(exc)
        except Exception as exc:  # noqa: BLE001 - surfaced to the operator
            return False, f"{type(exc).__name__}: {exc}"

    def _run_inner(self, steps: list, where: str) -> None:
        """Run nested steps, raising on the first failure.

        Nested steps do not get their own StepResult rows -- a 50-iteration
        stress loop would drown the table. The failure message carries the
        iteration and step so the row that does exist is still specific.
        """
        for index, step in enumerate(steps):
            if self._cancel.is_set():
                raise StepFailure("已取消")
            kind, spec, _ = self._parse(step, index)
            label = self._label(kind, spec)
            self.log("info", f"      {label}")
            started = time.monotonic()
            ok, message = self._invoke(kind, spec, label)
            self._mark = time.monotonic()
            self.log("info" if ok else "error", f"        {message}")
            if not ok:
                raise StepFailure(
                    f"{where} 第 {index + 1} 步（{label}）失败：{message}"
                    f"（{time.monotonic() - started:.2f}s）"
                )

    def _run_failure_handlers(self, handlers: list, outcome: StepResult) -> None:
        for index, step in enumerate(handlers):
            try:
                kind, spec, _ = self._parse(step, index)
            except FlowError as exc:
                self.log("error", f"  失败处理第 {index + 1} 项无法解析：{exc}")
                continue
            label = self._label(kind, spec)
            self.log("info", f"  失败处理 [{index + 1}/{len(handlers)}] {label}")
            try:
                self.log("info", f"    {self._handlers[kind](spec)}")
            except Exception as exc:  # noqa: BLE001
                # A handler that itself fails must not replace the reason the
                # run stopped -- that reason is the thing being debugged.
                self.log(
                    "error",
                    f"    失败处理出错（已忽略，原始失败：{outcome.message}）："
                    f"{type(exc).__name__}: {exc}",
                )

    def _start_device_screens(self) -> None:
        """Start every screen the device owns, before step 1.

        The detector needs about twenty contentful frames before it can judge
        anything. Starting a screen lazily at the step that first mentions it
        would push that calibration into the middle of the test, where a screen
        that is dark at that moment cannot be judged at all.
        """
        assert self._device is not None
        for name in self._device.screens:
            try:
                self.bench.start_screen(name, on_event=self._on_screen_event)
                self.log("info", f"    已开始监控屏幕 {name}")
            except Exception as exc:  # noqa: BLE001
                self.log(
                    "error",
                    f"    屏幕 {name} 启动失败，涉及它的检查会失败：{type(exc).__name__}: {exc}",
                )

    # -- step parsing -------------------------------------------------------

    @staticmethod
    def _parse(step: Any, index: int) -> tuple[str, Any, list | None]:
        if not isinstance(step, dict) or not step:
            raise FlowError(f"第 {index + 1} 步必须是一个映射，得到：{step!r}")
        payload = dict(step)
        on_failure = payload.pop("on_failure", None)
        if len(payload) != 1:
            raise FlowError(
                f"第 {index + 1} 步必须只含一个步骤键（可另带 on_failure），得到：{step!r}"
            )
        kind, spec = next(iter(payload.items()))
        if on_failure is not None and not isinstance(on_failure, list):
            raise FlowError(f"第 {index + 1} 步的 on_failure 必须是列表")
        return str(kind), spec, on_failure

    @staticmethod
    def _label(kind: str, spec: Any) -> str:
        names = {
            "log": "记录",
            "mark": "标记",
            "wait": "等待",
            "relay": "继电器",
            "serial_write": "串口发送",
            "serial_expect": "串口断言",
            "watch": "守护",
            "expect_watch": "守护判定",
            "adb": "ADB",
            "adb_wait": "等待 ADB",
            "ssh": "SSH",
            "expect_screen": "画面检查",
            "repeat": "循环",
            "snapshot": "截图存档",
            "adb_logs": "拉取日志",
        }
        head = names.get(kind, kind)
        if kind in ("log", "wait", "mark"):
            return f"{head} {spec}"
        if not isinstance(spec, dict):
            return head
        if kind == "repeat":
            return f"{head} {spec.get('name', '')} ×{spec.get('times', 1)}".strip()
        target = (
            spec.get("resource")
            or spec.get("console")
            or spec.get("screen")
            or spec.get("host")
            or spec.get("id")
        )
        detail = f" {target}" if target else ""
        action = spec.get("action") or spec.get("state")
        if action is not None:
            detail += f" {action}"
        elif spec.get("command"):
            detail += f" {spec['command']}"
        return f"{head}{detail}".strip()

    # -- device-aware helpers -----------------------------------------------

    def _require_device(self) -> DeviceCfg:
        if self._device is None:
            raise FlowError("流程没有声明 device")
        return self._device

    def _collector(self, name: str) -> LineCollector:
        collector = self._collectors.get(name)
        if collector is None:
            collector = LineCollector()
            self._collectors[name] = collector
        return collector

    def _since(self, spec: dict) -> float:
        """Where a step starts looking.

        ``step`` (the default) anchors at the end of the previous step, so a
        reply that arrives in the gap after a write is not missed while older
        output stays out. ``mark`` names an earlier point, which is what a check
        spanning several steps needs -- a power cycle is off, wait, on, and the
        blank it produces happens at the first of those.
        """
        anchor = str(spec.get("since", "step"))
        if anchor == "flow":
            return self._flow_start
        if anchor == "iteration":
            return self._iteration_mark
        if anchor == "step":
            return self._mark
        if anchor in self._named_marks:
            return self._named_marks[anchor]
        raise StepFailure(
            f"since: {anchor} 不是已知锚点（可用 step / iteration / flow "
            f"或 mark 步骤起过的名字：{', '.join(self._named_marks) or '无'}）"
        )

    # -- evidence -----------------------------------------------------------

    def _incident_dir(self) -> Path:
        """One directory per failed run, created on first use."""
        if self._incident is None:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            device = self._device.name if self._device else "device"
            self._incident = EVIDENCE_DIR / f"{stamp}_{device}_{self._flow_name}"
            self._incident.mkdir(parents=True, exist_ok=True)
            (self._incident / "summary.txt").write_text(
                self._summary_text(), encoding="utf-8"
            )
        return self._incident

    def _summary_text(self) -> str:
        """What someone debugging this at 9am tomorrow actually needs.

        The physical binding is the point: a flow talks about KL30 and 屏幕2, and
        whoever reads this needs to know that meant relay1 channel 9 and camera
        index 5, without opening the settings file.
        """
        lines = [
            f"流程      : {self._flow_name}",
            f"设备      : {self._device.name if self._device else '?'}",
            f"开始时间  : {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self._flow_start))}",
            "",
            "解析后的物理绑定：",
        ]
        if self._device is not None:
            lines.append(f"  ADB      : {self._device.adb_serial or '（未绑定）'}")
            for name in self._device.power:
                try:
                    channel = self.bench.resolve(self._device.name, "power", name)
                    lines.append(
                        f"  电源     : {name} -> 板卡 {channel.relay} 第 {channel.channel} 路"
                    )
                except BenchError as exc:
                    lines.append(f"  电源     : {name} -> {exc}")
            for name in self._device.consoles:
                try:
                    console = self.bench.resolve(self._device.name, "consoles", name)
                    lines.append(
                        f"  串口     : {name} -> {console.port or '（未绑定端口）'} @ {console.baudrate}"
                    )
                except BenchError as exc:
                    lines.append(f"  串口     : {name} -> {exc}")
            for name in self._device.screens:
                try:
                    screen = self.bench.resolve(self._device.name, "screens", name)
                    camera = self.bench.camera_cfg(screen.camera)
                    lines.append(
                        f"  画面     : {name} -> 摄像头 {screen.camera}"
                        f"（源 {camera.source}）ROI "
                        f"x={screen.roi.x:.3f} y={screen.roi.y:.3f} "
                        f"w={screen.roi.w:.3f} h={screen.roi.h:.3f}"
                    )
                except BenchError as exc:
                    lines.append(f"  画面     : {name} -> {exc}")
        lines.append("")
        for watch_id, watcher in self._watches.items():
            first = watcher.first()
            when = (
                time.strftime("%H:%M:%S", time.localtime(first.wall)) if first else "—"
            )
            lines.append(
                f"守护 {watch_id}（{watcher.console} {watcher.pattern.pattern!r}）："
                f"命中 {watcher.count()} 次，首次 {when}"
            )
            if first is not None:
                lines.append(f"    {first.line}")
        return "\n".join(lines) + "\n"

    def _tail_console(self, name: str, lines: int = 200) -> str:
        collector = self._collectors.get(name)
        if collector is None:
            return "（本次运行没有采集到这个串口的输出）"
        text = collector.text()
        return "\n".join(text.splitlines()[-lines:]) or "（无输出）"

    # -- steps: bench control -----------------------------------------------

    def _step_log(self, spec: Any) -> str:
        text = str(spec)
        self.log("info", f"    {text}")
        return text

    def _step_mark(self, spec: Any) -> str:
        """Name the current moment, so a later step can look back to it."""
        name = str(spec)
        if not name:
            raise StepFailure("mark 需要一个名字")
        self._named_marks[name] = time.monotonic()
        return f"已标记 {name}"

    def _step_wait(self, spec: Any) -> str:
        seconds = float(spec)
        if seconds < 0:
            raise StepFailure("等待时长不能为负")
        _sleep(seconds, self._cancel)
        if self._cancel.is_set():
            raise StepFailure("已取消")
        return f"等待 {seconds}s"

    def _step_relay(self, spec: Any) -> str:
        if not isinstance(spec, dict):
            raise StepFailure("relay 需要一个映射，例如 {resource: KL30, action: \"on\"}")
        device = self._require_device()
        action = _action(spec)
        resource = spec.get("resource")

        if resource is None:
            if action == "all_off":
                return self._set_rails(device.power, False)
            if action == "all_on":
                return self._set_rails(device.power, True)
            if action == "read":
                return self._read_rails(device.power)
            raise StepFailure(
                "relay 需要指定 resource，或在不指定时用 action: all_off / all_on / read"
            )

        board, channel = self.bench.power(device.name, resource)
        if action == "on":
            board.set_channel(channel.channel, True)
            return f"{resource}（{channel.relay} CH{channel.channel}）闭合"
        if action == "off":
            board.set_channel(channel.channel, False)
            return f"{resource}（{channel.relay} CH{channel.channel}）断开"
        if action == "pulse":
            milliseconds = int(spec.get("pulse_ms", channel.pulse_ms))
            board.pulse(channel.channel, milliseconds)
            return f"{resource}（{channel.relay} CH{channel.channel}）脉冲 {milliseconds}ms"
        if action == "read":
            states = board.read_channels()
            closed = [
                i + 1 for i, state in enumerate(states) if state
            ]
            return f"{channel.relay} 闭合通道：{closed or '无'}"
        raise StepFailure(
            f"未知的 relay action：{action}（可用：on/off/pulse/read/all_on/all_off）"
        )

    def _set_rails(self, names: list[str], on: bool) -> str:
        """Switch a set of rails, reporting every failure.

        Scoped to the names given rather than to the board: two devices can
        share a board, and a board-wide ``all_off`` would drop the other
        device's power as a side effect of this one being finished with.
        """
        device = self._require_device()
        if not names:
            return "该设备没有声明任何继电器资源"
        problems: list[str] = []
        switched: list[str] = []
        for name in names:
            try:
                board, channel = self.bench.power(device.name, name)
                board.set_channel(channel.channel, on)
                switched.append(name)
            except Exception as exc:  # noqa: BLE001 - collected for the report
                problems.append(f"{name}：{exc}")
        if problems:
            raise StepFailure("；".join(problems))
        return f"{', '.join(switched)} 全部{'闭合' if on else '断开'}"

    def _read_rails(self, names: list[str]) -> str:
        device = self._require_device()
        parts = []
        for name in names:
            board, channel = self.bench.power(device.name, name)
            states = board.read_channels()
            index = channel.channel - 1
            state = states[index] if index < len(states) else None
            parts.append(f"{name}={'闭合' if state else '断开'}")
        return "，".join(parts) if parts else "该设备没有声明任何继电器资源"

    # -- steps: consoles ----------------------------------------------------

    def _step_serial_write(self, spec: Any) -> str:
        if not isinstance(spec, dict):
            raise StepFailure('serial_write 需要一个映射，例如 {text: "AT\\r\\n"}')
        device = self._require_device()
        name = spec.get("console")
        if not name:
            raise StepFailure("serial_write 需要 console")
        link = self.bench.device_console(
            device.name, name, on_line=self._collector(name)
        )

        if spec.get("hex") is not None:
            raw = bytes.fromhex(str(spec["hex"]).replace(" ", ""))
            link.write_bytes(raw)
            return f"{name} 发送 {len(raw)} 字节 {raw.hex(' ')}"

        text = str(spec.get("text", ""))
        if not text:
            raise StepFailure("serial_write 需要 text 或 hex")
        link.write(text)
        return f"{name} 发送 {text!r}"

    def _step_serial_expect(self, spec: Any) -> str:
        if not isinstance(spec, dict):
            raise StepFailure('serial_expect 需要一个映射，例如 {contains: "ready"}')
        device = self._require_device()
        name = spec.get("console")
        if not name:
            raise StepFailure("serial_expect 需要 console")
        collector = self._collector(name)
        self.bench.device_console(device.name, name, on_line=collector)

        pattern = _build_pattern(spec)
        if pattern is None:
            raise StepFailure("serial_expect 需要 contains / regex / expect 之一")
        absent = bool(spec.get("absent", False))
        timeout = float(spec.get("timeout", 10.0))
        since = self._since(spec)
        deadline = time.monotonic() + timeout

        while True:
            text = collector.text(since)
            match = pattern.search(text)
            if absent:
                if match:
                    raise StepFailure(
                        f"{name} 出现不应出现的 {pattern.pattern!r}：{match.group(0)!r}"
                    )
                if time.monotonic() >= deadline:
                    return f"{name} 在窗口内未出现 {pattern.pattern!r}"
            else:
                if match:
                    return f"{name} 收到 {match.group(0)!r}"
                if time.monotonic() >= deadline:
                    tail = "\n".join(text.splitlines()[-8:]) or "（无输出）"
                    raise StepFailure(
                        f"{name} 在 {timeout}s 内未匹配 {pattern.pattern!r}，"
                        f"最近输出：\n{tail}"
                    )
            if self._cancel.is_set():
                raise StepFailure("已取消")
            time.sleep(0.05)

    # -- steps: background watches ------------------------------------------

    def _step_watch(self, spec: Any) -> str:
        if not isinstance(spec, dict):
            raise StepFailure("watch 需要一个映射，例如 {id: mcu_reset, console: MCU, regex: ...}")
        device = self._require_device()
        name = spec.get("console")
        if not name:
            raise StepFailure("watch 需要 console")
        pattern = _build_pattern(spec)
        if pattern is None:
            raise StepFailure("watch 需要 contains / regex / expect 之一")
        watch_id = str(spec.get("id") or f"{name}:{pattern.pattern}")

        watcher = Watcher(watch_id, name, pattern)
        self._watches[watch_id] = watcher
        self.bench.device_console(device.name, name, on_line=watcher)
        return f"开始守护 {name}：{pattern.pattern!r}（标识 {watch_id}）"

    def _step_expect_watch(self, spec: Any) -> str:
        if not isinstance(spec, dict):
            raise StepFailure("expect_watch 需要一个映射，例如 {id: mcu_reset, at_most: 0}")
        watch_id = str(spec.get("id", ""))
        watcher = self._watches.get(watch_id)
        if watcher is None:
            known = ", ".join(self._watches) or "（无）"
            raise StepFailure(f"没有名为「{watch_id}」的守护，先挂上它。当前：{known}")

        count = watcher.count()
        at_most = spec.get("at_most")
        at_least = spec.get("at_least")

        if at_most is not None and count > int(at_most):
            first = watcher.first()
            when = (
                time.strftime("%H:%M:%S", time.localtime(first.wall)) if first else "?"
            )
            raise StepFailure(
                f"{watcher.console} 出现 {count} 次不应出现的 "
                f"{watcher.pattern.pattern!r}（上限 {at_most}）；"
                f"首次 {when}：{first.line if first else ''}"
            )
        if at_least is not None and count < int(at_least):
            raise StepFailure(
                f"{watcher.console} 只命中 {count} 次 {watcher.pattern.pattern!r}，"
                f"要求至少 {at_least} 次"
            )
        return f"{watcher.console} 命中 {count} 次 {watcher.pattern.pattern!r}"

    # -- steps: adb ---------------------------------------------------------

    def _step_adb(self, spec: Any) -> str:
        if not isinstance(spec, dict):
            raise StepFailure('adb 需要一个映射，例如 {command: "shell id"}')
        device = self._require_device()
        command = spec.get("command")
        if not command:
            raise StepFailure("adb 需要 command")
        client = self.bench.adb(device.name)

        output = client.run(command, timeout=float(spec.get("timeout", 30.0)))
        for line in output.splitlines():
            self.log("info", f"    | {line}")
        return self._check_output(spec, output, f"adb {command}")

    def _step_adb_wait(self, spec: Any) -> str:
        """Wait for the device to enumerate *and* answer a shell command.

        ``adb devices`` alone is not enough: a device appears in the list well
        before its shell is usable, and a test that starts stepping at that
        moment fails for a reason that has nothing to do with what it tests.
        """
        if not isinstance(spec, dict):
            spec = {"timeout": spec}
        device = self._require_device()
        timeout = float(spec.get("timeout", 60.0))
        client = self.bench.adb(device.name)
        serial = device.adb_serial
        deadline = time.monotonic() + timeout
        last = "尚未发现设备"

        while time.monotonic() < deadline:
            if self._cancel.is_set():
                raise StepFailure("已取消")
            remaining = max(1.0, deadline - time.monotonic())
            try:
                devices = client.list_devices(timeout=min(5.0, remaining))
                ready = [
                    d for d in devices if d.is_ready and (not serial or d.serial == serial)
                ]
                if not ready:
                    last = f"adb 列表里没有就绪设备（{', '.join(d.serial for d in devices) or '空'}）"
                else:
                    probe = client.run("shell echo __gear_ready__", timeout=min(8.0, remaining))
                    if "__gear_ready__" in probe:
                        return f"{ready[0].serial} shell 就绪"
                    last = probe.strip() or "shell 无输出"
            except AdbError as exc:
                last = str(exc)
            _sleep(0.5, self._cancel)

        raise StepFailure(f"{timeout}s 内未等到 {serial or '设备'} 的 shell 就绪：{last}")

    def _step_adb_logs(self, spec: Any) -> str:
        device = self._require_device()
        if not isinstance(spec, dict):
            spec = {}
        commands = spec.get("commands") or [
            "logcat -d -v time",
            "shell dmesg",
            "shell getprop",
        ]
        timeout = float(spec.get("timeout", 60.0))
        directory = self._incident_dir() / "adb"
        directory.mkdir(parents=True, exist_ok=True)
        client = self.bench.adb(device.name)

        written = []
        for index, command in enumerate(commands):
            safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(command))[:60]
            path = directory / f"{index:02d}_{safe}.txt"
            try:
                text = client.run(command, timeout=timeout)
            except Exception as exc:  # noqa: BLE001 - recorded into the file
                text = f"（命令失败：{type(exc).__name__}: {exc}）\n"
            path.write_text(text, encoding="utf-8", errors="replace")
            written.append(path.name)
        self.log("info", f"    已写入 {', '.join(written)}")
        return f"已拉取 {len(written)} 项 adb 输出到 {directory}"

    # -- steps: ssh ---------------------------------------------------------

    def _step_ssh(self, spec: Any) -> str:
        if not isinstance(spec, dict):
            raise StepFailure('ssh 需要一个映射，例如 {command: "make"}')
        name = spec.get("host")
        if not name:
            if len(self.bench.settings.ssh) != 1:
                known = ", ".join(h.name for h in self.bench.settings.ssh) or "（无）"
                raise StepFailure(f"ssh 需要 host，已配置：{known}")
            name = self.bench.settings.ssh[0].name
        host = self.bench.ssh(name)
        command = spec.get("command")
        if not command:
            raise StepFailure("ssh 需要 command")

        output = host.run(command, timeout=float(spec.get("timeout", 120.0)))
        for line in output.splitlines():
            self.log("info", f"    | {line}")
        return self._check_output(spec, output, f"ssh {command}")

    @staticmethod
    def _check_output(spec: dict, output: str, what: str) -> str:
        pattern = _build_pattern(spec)
        if pattern is None:
            return f"{what} 完成"
        match = pattern.search(output)
        if spec.get("absent"):
            if match:
                raise StepFailure(f"{what} 输出出现不应出现的 {match.group(0)!r}")
            return f"{what}：未出现 {pattern.pattern!r}"
        if not match:
            tail = "\n".join(output.splitlines()[-8:]) or "（无输出）"
            raise StepFailure(f"{what} 输出未匹配 {pattern.pattern!r}，实际：\n{tail}")
        return f"{what}：匹配到 {match.group(0)!r}"

    # -- steps: screens -----------------------------------------------------

    def _step_expect_screen(self, spec: Any) -> str:
        if not isinstance(spec, dict):
            raise StepFailure("expect_screen 需要一个映射，例如 {screen: 屏幕2, state: lit}")
        device = self._require_device()
        name = spec.get("screen")
        if not name:
            raise StepFailure("expect_screen 需要 screen")
        monitor = self.bench.device_screen(device.name, name)
        if monitor.hub is None or not monitor.hub.running:
            if monitor.hub is not None:
                monitor.hub.start()

        sequence = spec.get("sequence")
        if sequence:
            return self._expect_sequence(
                monitor, name, list(sequence), float(spec.get("timeout", 20.0)), spec
            )

        state = str(spec.get("state", "lit"))
        timeout = float(spec.get("timeout", spec.get("duration", 20.0)))
        since = self._since(spec)

        if state in ("lit", "not_black", "亮屏"):
            return self._expect_lit(monitor, name, since, timeout)
        if state in ("dark", "blanked", "变黑"):
            return self._expect_event(
                monitor, name, since, timeout, DARK_TYPES, "变黑（含闪黑）"
            )
        if state in ("black", "黑屏"):
            return self._expect_event(
                monitor, name, since, timeout, (EventType.BLACK_SCREEN,), "持续黑屏"
            )
        if state in ("freeze", "冻屏"):
            return self._expect_event(
                monitor, name, since, timeout, (EventType.FREEZE,), "冻屏"
            )
        if state in ("no_freeze", "无冻屏"):
            return self._expect_absent(
                monitor, name, since, timeout, (EventType.FREEZE,), "冻屏"
            )
        if state in ("no_black", "无黑屏"):
            return self._expect_absent(
                monitor, name, since, timeout, DARK_TYPES, "黑屏或闪黑"
            )
        raise StepFailure(
            f"未知的画面状态「{state}」"
            "（可用：lit / dark / black / freeze / no_freeze / no_black，或 sequence）"
        )

    def _expect_lit(self, monitor, name: str, since: float, window: float) -> str:
        """The screen must show content for the whole window.

        Refuses to answer before the detector has a brightness baseline. Without
        that it cannot call anything dark -- ``_is_dark`` returns False while the
        baseline is unset -- so a panel that never powered up would sail through
        a "check it lit up" step. Running out of window without a baseline is
        itself the finding: nothing judgeable ever appeared.
        """
        deadline = time.monotonic() + window
        while True:
            dark = [e for e in monitor.events_since(since) if e.type in DARK_TYPES]
            if dark:
                raise StepFailure(f"屏幕「{name}」出现{dark[0].label}：{dark[0].detail}")
            events = monitor.events_since(since)
            stalled = [e for e in events if e.type is EventType.CAPTURE_STALL]
            if stalled:
                raise StepFailure(f"屏幕「{name}」摄像头采集停滞，无法判定")
            if time.monotonic() >= deadline:
                break
            if self._cancel.is_set():
                raise StepFailure("已取消")
            time.sleep(0.1)

        if not monitor.calibrated:
            state = monitor.snapshot()
            raise StepFailure(
                f"屏幕「{name}」在 {window}s 内没有出现可判定的画面内容"
                f"（亮度基线未建立）。摄像头可能没对准、屏幕没有点亮或未上电；"
                f"当前平坦度 {state.get('flatness')}、亮度 {state.get('brightness')}"
            )
        return f"屏幕「{name}」{window}s 内保持有内容"

    def _expect_event(
        self,
        monitor,
        name: str,
        since: float,
        timeout: float,
        types: tuple,
        what: str,
    ) -> str:
        deadline = time.monotonic() + timeout
        while True:
            found = [e for e in monitor.events_since(since) if e.type in types]
            if found:
                event = found[0]
                duration = event.duration
                extra = f"（{duration:.2f}s）" if duration else ""
                return f"屏幕「{name}」出现{what}{extra}：{event.detail}"
            if time.monotonic() >= deadline:
                raise StepFailure(f"屏幕「{name}」{timeout}s 内未出现{what}")
            if self._cancel.is_set():
                raise StepFailure("已取消")
            time.sleep(0.1)

    def _expect_absent(
        self,
        monitor,
        name: str,
        since: float,
        window: float,
        types: tuple,
        what: str,
    ) -> str:
        deadline = time.monotonic() + window
        while True:
            found = [e for e in monitor.events_since(since) if e.type in types]
            if found:
                raise StepFailure(f"屏幕「{name}」出现{what}：{found[0].detail}")
            if time.monotonic() >= deadline:
                return f"屏幕「{name}」{window}s 内未出现{what}"
            if self._cancel.is_set():
                raise StepFailure("已取消")
            time.sleep(0.1)

    def _expect_sequence(
        self, monitor, name: str, states: list, timeout: float, spec: dict
    ) -> str:
        """A transition: the screen must go dark, then come back.

        Written as one step rather than two ``expect_screen`` steps because
        there is a blind gap between two consecutive steps -- the first returns
        the instant it sees the screen go dark and the second has not started
        watching yet -- and because a quick blank-and-recover can begin and end
        inside that gap, leaving both timestamps before the second step's window.
        Anchoring the recovery on the detector's own RECOVERED event also gives
        the exact moment the screen came back.
        """
        if states[:2] != ["dark", "lit"]:
            raise StepFailure(
                f"目前只支持 sequence: [dark, lit]，收到 {states}"
            )
        since = self._since(spec)
        deadline = time.monotonic() + timeout

        dark = None
        while time.monotonic() < deadline:
            found = [e for e in monitor.events_since(since) if e.type in DARK_TYPES]
            if found:
                dark = found[0]
                break
            if self._cancel.is_set():
                raise StepFailure("已取消")
            time.sleep(0.05)
        if dark is None:
            raise StepFailure(f"屏幕「{name}」{timeout}s 内没有变黑")

        if dark.type is EventType.TRANSIENT_BLACKOUT:
            return f"屏幕「{name}」闪黑 {(dark.duration or 0) * 1000:.0f}ms 后恢复"

        recover_deadline = time.monotonic() + timeout
        while time.monotonic() < recover_deadline:
            recovered = [
                e
                for e in monitor.events_since(dark.t_start)
                if e.type is EventType.RECOVERED
                and e.metrics.get("was") == "black_screen"
            ]
            if recovered:
                interval = (recovered[0].t_end or recovered[0].t_start) - dark.t_start
                fresh = [
                    e
                    for e in monitor.events_since(recovered[0].t_end or dark.t_start)
                    if e.type in DARK_TYPES
                ]
                if fresh:
                    raise StepFailure(
                        f"屏幕「{name}」恢复后又变黑：{fresh[0].detail}"
                    )
                return f"屏幕「{name}」黑屏 {interval:.2f}s 后恢复亮屏"
            if self._cancel.is_set():
                raise StepFailure("已取消")
            time.sleep(0.1)

        raise StepFailure(
            f"屏幕「{name}」变黑后在 {timeout}s 内没有恢复（上电后长时间不亮屏）"
        )

    def _step_snapshot(self, spec: Any) -> str:
        import cv2

        device = self._require_device()
        if not isinstance(spec, dict):
            spec = {}
        which = spec.get("screens", "all")
        names = list(device.screens) if which in ("all", None) else list(which)
        note = str(spec.get("note", ""))
        directory = self._incident_dir() / "screens"
        directory.mkdir(parents=True, exist_ok=True)

        saved: list[str] = []
        for name in names:
            try:
                monitor = self.bench.device_screen(device.name, name)
            except BenchError as exc:
                self.log("error", f"    {name}：{exc}")
                continue

            frame = monitor.latest_frame()
            if frame is not None:
                path = directory / f"{name}_current.jpg"
                cv2.imwrite(str(path), frame, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
                saved.append(path.name)

            # The ring is what makes a transient visible after the fact; the
            # current frame alone cannot show a 150ms blank that is already over.
            now = time.monotonic()
            written = monitor.evidence.save(
                directory, name, "snapshot", now - 2.0, now, pad=0.0
            )
            if written is not None:
                saved.append(f"{name}/（{len(list(written.glob('*.jpg')))} 帧环形缓冲）")

        if note:
            (directory / "note.txt").write_text(note, encoding="utf-8")

        # Console tails go in the same bundle: a screen that failed to light is
        # usually explained by what the consoles were printing at the time.
        for console in device.consoles:
            text = self._tail_console(console)
            (directory / f"console_{console}.txt").write_text(text, encoding="utf-8")

        return f"已保存 {len(saved)} 项画面证据到 {directory}" + (f"（{note}）" if note else "")

    # -- steps: loops -------------------------------------------------------

    def _step_repeat(self, spec: Any) -> str:
        if not isinstance(spec, dict):
            raise StepFailure("repeat 需要一个映射，例如 {times: 50, steps: [...]}")
        times = int(spec.get("times", 1))
        steps = spec.get("steps")
        if not isinstance(steps, list) or not steps:
            raise StepFailure("repeat 需要非空的 steps 列表")
        if times < 1:
            raise StepFailure("repeat 的 times 必须 >= 1")

        label = str(spec.get("name") or "循环")
        completed = 0
        for iteration in range(times):
            if self._cancel.is_set():
                raise StepFailure(f"{label} 在第 {iteration + 1} 轮被取消")
            self._iteration_mark = time.monotonic()
            self.log("info", f"    {label} 第 {iteration + 1}/{times} 轮")
            self._run_inner(steps, f"{label} 第 {iteration + 1}/{times} 轮")
            completed += 1

        return f"{label} 完成 {completed}/{times} 轮"
