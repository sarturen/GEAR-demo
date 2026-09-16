"""Running blocking work off the GUI thread.

Every device call in this bench blocks -- a serial transaction waits on a reply,
``adb`` waits on a subprocess, a camera probe waits on a driver. None of them may
run on the thread that paints the window, so panels hand their work to this.
"""

from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal


class _Signals(QObject):
    done = Signal(object)
    failed = Signal(str)


class _Task(QRunnable):
    def __init__(self, fn: Callable[[], Any]) -> None:
        super().__init__()
        self._fn = fn
        self.signals = _Signals()
        self.setAutoDelete(True)

    def run(self) -> None:
        try:
            result = self._fn()
        except Exception as exc:  # noqa: BLE001 - reported to the operator
            self._report(self.signals.failed, f"{type(exc).__name__}: {exc}")
        else:
            self._report(self.signals.done, result)

    def _report(self, signal: Signal, payload: Any) -> None:
        """Deliver a result unless the window it was meant for is already gone.

        A task can still be in flight when the application closes. Dropping its
        result then is correct; letting the emit raise would surface as a
        traceback during interpreter shutdown.
        """
        try:
            signal.emit(payload)
        except RuntimeError:
            pass


#: QThreadPool does not keep a Python reference to its runnables, so without
#: this a task can be collected before it ever runs.
_LIVE: set[_Task] = set()


def run_task(
    fn: Callable[[], Any],
    on_done: Callable[[Any], None] | None = None,
    on_error: Callable[[str], None] | None = None,
) -> None:
    """Run ``fn`` on the global thread pool and report back on the GUI thread.

    Callbacks run in the GUI thread, so they may touch widgets directly.
    """
    task = _Task(fn)
    _LIVE.add(task)

    def release(*_: Any) -> None:
        _LIVE.discard(task)

    task.signals.done.connect(release)
    task.signals.failed.connect(release)
    if on_done is not None:
        task.signals.done.connect(on_done)
    if on_error is not None:
        task.signals.failed.connect(on_error)
    QThreadPool.globalInstance().start(task)


def drain(timeout_ms: int = 5000) -> bool:
    """Wait for in-flight tasks. Call before tearing down what they touch."""
    return QThreadPool.globalInstance().waitForDone(timeout_ms)
