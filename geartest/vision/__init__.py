"""Statistical core of the on-screen anomaly detector."""

from .detector import AnomalyDetector, Event, EventType, Severity
from .evidence import EvidenceRing
from .invariants import FrameStats, analyse, max_channel
from .monitor import CameraHub, ScreenMonitor, roi_to_pixels

__all__ = [
    "FrameStats",
    "analyse",
    "max_channel",
    "AnomalyDetector",
    "Event",
    "EventType",
    "Severity",
    "EvidenceRing",
    "CameraHub",
    "ScreenMonitor",
    "roi_to_pixels",
]
