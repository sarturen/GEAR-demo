"""Live camera preview with a drag-to-select monitor region."""

from __future__ import annotations

import cv2
import numpy as np
from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from ..config import RoiCfg

ROI_OUTLINE = "#00d0ff"
BACKDROP = "#1e1e1e"


def _outside_bands(outer: QRect, inner: QRect):
    """The four strips of ``outer`` not covered by ``inner``, for dimming."""
    inner = inner.intersected(outer)
    if inner.isEmpty():
        return
    yield QRect(outer.x(), outer.y(), outer.width(), inner.y() - outer.y())
    yield QRect(outer.x(), inner.bottom() + 1, outer.width(), outer.bottom() - inner.bottom())
    yield QRect(outer.x(), inner.y(), inner.x() - outer.x(), inner.height())
    yield QRect(inner.right() + 1, inner.y(), outer.right() - inner.right(), inner.height())


class VideoView(QWidget):
    """Shows the latest frame and lets the user drag out the monitored region.

    The region is kept as fractions of the frame, so it survives the panel being
    resized and the camera being swapped for one with a different resolution.
    """

    roi_changed = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(480, 300)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMouseTracking(True)
        self._image: QImage | None = None
        self._roi = RoiCfg()
        self._drag_origin: QPoint | None = None
        self._drag_current: QPoint | None = None
        self.placeholder = "未连接摄像头"

    # -- content ------------------------------------------------------------

    def set_frame(self, frame_bgr: np.ndarray | None) -> None:
        if frame_bgr is None:
            self._image = None
        else:
            rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
            height, width, channels = rgb.shape
            # copy() so the image owns its pixels: the capture thread hands the
            # same buffer on the moment this returns.
            self._image = QImage(
                rgb.data, width, height, channels * width, QImage.Format_RGB888
            ).copy()
        self.update()

    def set_roi(self, roi: RoiCfg) -> None:
        self._roi = RoiCfg(roi.x, roi.y, roi.w, roi.h)
        self.update()

    def roi(self) -> RoiCfg:
        return RoiCfg(self._roi.x, self._roi.y, self._roi.w, self._roi.h)

    def reset_roi(self) -> None:
        self._roi = RoiCfg()
        self.roi_changed.emit(self.roi())
        self.update()

    # -- geometry -----------------------------------------------------------

    def _image_rect(self) -> QRect:
        if self._image is None or self._image.isNull():
            return QRect()
        iw, ih = self._image.width(), self._image.height()
        scale = min(self.width() / iw, self.height() / ih)
        tw, th = max(1, int(iw * scale)), max(1, int(ih * scale))
        return QRect((self.width() - tw) // 2, (self.height() - th) // 2, tw, th)

    def _norm_to_widget(self, x: float, y: float, w: float, h: float) -> QRect:
        rect = self._image_rect()
        return QRect(
            rect.x() + int(x * rect.width()),
            rect.y() + int(y * rect.height()),
            max(1, int(w * rect.width())),
            max(1, int(h * rect.height())),
        )

    def _widget_to_norm(self, point: QPoint) -> tuple[float, float]:
        rect = self._image_rect()
        if rect.isEmpty():
            return 0.0, 0.0
        return (
            max(0.0, min(1.0, (point.x() - rect.x()) / rect.width())),
            max(0.0, min(1.0, (point.y() - rect.y()) / rect.height())),
        )

    # -- painting -----------------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(BACKDROP))
        image_rect = self._image_rect()
        if self._image is None or image_rect.isEmpty():
            painter.setPen(QColor("#909090"))
            painter.drawText(self.rect(), Qt.AlignCenter, self.placeholder)
            return

        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        painter.drawImage(image_rect, self._image)

        if self._drag_origin is not None and self._drag_current is not None:
            roi_rect = QRect(self._drag_origin, self._drag_current).normalized()
        else:
            roi_rect = self._norm_to_widget(self._roi.x, self._roi.y, self._roi.w, self._roi.h)

        painter.setBrush(QColor(0, 0, 0, 90))
        painter.setPen(Qt.NoPen)
        for band in _outside_bands(image_rect, roi_rect):
            painter.drawRect(band)

        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(QColor(ROI_OUTLINE), 2))
        painter.drawRect(roi_rect)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self.update()

    # -- selection ----------------------------------------------------------

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.button() != Qt.LeftButton or self._image is None:
            return
        point = event.position().toPoint()
        if not self._image_rect().contains(point):
            return
        self._drag_origin = point
        self._drag_current = point
        self.update()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self._drag_origin is None:
            return
        self._drag_current = event.position().toPoint()
        self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self._drag_origin is None or event.button() != Qt.LeftButton:
            return
        start, end = self._drag_origin, event.position().toPoint()
        self._drag_origin = None
        self._drag_current = None

        x1, y1 = self._widget_to_norm(start)
        x2, y2 = self._widget_to_norm(end)
        x, y = min(x1, x2), min(y1, y2)
        w, h = abs(x2 - x1), abs(y2 - y1)
        if w < 0.02 or h < 0.02:
            # A stray click, not a selection.
            self.update()
            return

        self._roi = RoiCfg(round(x, 4), round(y, 4), round(w, 4), round(h, 4))
        self.roi_changed.emit(self.roi())
        self.update()
