"""Turning manual bench operations into a replayable flow.

The operator works the panels by hand once; what they did is captured as flow
steps. Only actions that map onto a step type are recorded, and only while
recording is armed, so ordinary fiddling does not pollute the flow.
"""

from __future__ import annotations

import threading

import yaml


class Recorder:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._enabled = False
        self._steps: list[dict] = []

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    def set_enabled(self, value: bool) -> None:
        with self._lock:
            self._enabled = value

    def clear(self) -> None:
        with self._lock:
            self._steps.clear()

    @property
    def steps(self) -> list[dict]:
        with self._lock:
            return list(self._steps)

    def __len__(self) -> int:
        with self._lock:
            return len(self._steps)

    def record(self, kind: str, spec: dict) -> None:
        with self._lock:
            if self._enabled:
                self._steps.append({kind: spec})

    def to_flow(self, name: str) -> dict:
        return {"name": name, "steps": self.steps}

    def to_yaml(self, name: str) -> str:
        return yaml.safe_dump(
            self.to_flow(name), allow_unicode=True, sort_keys=False, default_flow_style=False
        )
