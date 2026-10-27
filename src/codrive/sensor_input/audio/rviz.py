"""ROS 2 debug publishers for audio events and log-mel images."""

from __future__ import annotations

from typing import Any

import numpy as np

from ...contracts.models import AudioKind
from .features import feature_to_mono8
from .worker import AudioInferenceResult


def waveform_envelope(
    waveform: np.ndarray, *, bucket_count: int = 256
) -> tuple[np.ndarray, np.ndarray]:
    """Return normalized x and min/max y points for a readable live waveform."""

    values = np.asarray(waveform, dtype=np.float32).reshape(-1)
    if bucket_count <= 0:
        raise ValueError("bucket_count must be positive")
    if values.size == 0:
        raise ValueError("waveform must not be empty")
    if not np.isfinite(values).all():
        raise ValueError("waveform contains non-finite values")
    if values.size < bucket_count:
        return (
            np.linspace(0.0, 1.0, values.size, dtype=np.float32),
            np.clip(values, -1.0, 1.0),
        )
    samples_per_bucket = values.size // bucket_count
    trimmed = values[: samples_per_bucket * bucket_count].reshape(
        bucket_count, samples_per_bucket
    )
    minimum = trimmed.min(axis=1)
    maximum = trimmed.max(axis=1)
    x = np.repeat(
        (np.arange(bucket_count, dtype=np.float32) + 0.5) / bucket_count, 2
    )
    y = np.column_stack((minimum, maximum)).reshape(-1)
    return x, np.clip(y, -1.0, 1.0).astype(np.float32, copy=False)


def waveform_levels(waveform: np.ndarray) -> tuple[float, float, float]:
    """Return RMS, peak, and RMS dBFS for display diagnostics."""

    values = np.asarray(waveform, dtype=np.float32).reshape(-1)
    if values.size == 0 or not np.isfinite(values).all():
        raise ValueError("waveform must contain finite samples")
    rms = float(np.sqrt(np.mean(np.square(values, dtype=np.float64))))
    peak = float(np.max(np.abs(values)))
    dbfs = 20.0 * float(np.log10(max(rms, 1e-6)))
    return rms, peak, max(-120.0, dbfs)


class RosAudioVisualizer:
    """Publish only standard ROS messages, so no custom message build is needed."""

    _BACKGROUND = (0.035, 0.055, 0.090)
    _PANEL = (0.075, 0.105, 0.160)
    _TRACK = (0.140, 0.180, 0.245)
    _TEXT = (0.910, 0.945, 0.980)
    _MUTED = (0.500, 0.590, 0.690)
    _CLEAR = (0.130, 0.820, 0.650)
    _SIREN = (1.000, 0.210, 0.240)
    _HORN = (1.000, 0.670, 0.100)
    _WAVE = (0.200, 0.660, 1.000)

    def __init__(
        self,
        *,
        fixed_frame: str = "base_link",
        siren_threshold: float = 0.72,
        horn_threshold: float = 0.72,
        horn_enabled: bool = True,
    ) -> None:
        try:
            import rclpy
            from rclpy.node import Node
            from sensor_msgs.msg import Image
            from std_msgs.msg import Float32MultiArray
            from geometry_msgs.msg import Point
            from visualization_msgs.msg import Marker, MarkerArray
        except ImportError as exc:
            raise RuntimeError(
                "ROS 2 Python packages are unavailable; run "
                "'source /opt/ros/humble/setup.bash' first"
            ) from exc
        self._rclpy = rclpy
        self._Image = Image
        self._Float32MultiArray = Float32MultiArray
        self._Point = Point
        self._Marker = Marker
        self._MarkerArray = MarkerArray
        self._owns_context = not rclpy.ok()
        if self._owns_context:
            rclpy.init(args=None)
        self.node: Any = Node("codrive_audio_visualizer")
        self.fixed_frame = fixed_frame
        self.siren_threshold = siren_threshold
        self.horn_threshold = horn_threshold
        self.horn_enabled = horn_enabled
        self._dashboard_initialized = False
        self.marker_publisher = self.node.create_publisher(
            MarkerArray, "/codrive/audio/markers", 10
        )
        self.image_publisher = self.node.create_publisher(
            Image, "/codrive/audio/spectrogram", 10
        )
        self.probability_publisher = self.node.create_publisher(
            Float32MultiArray, "/codrive/audio/probabilities", 10
        )

    @staticmethod
    def _color(kind: AudioKind) -> tuple[float, float, float]:
        if kind is AudioKind.SIREN:
            return RosAudioVisualizer._SIREN
        if kind is AudioKind.HORN:
            return RosAudioVisualizer._HORN
        return RosAudioVisualizer._CLEAR

    def _marker(self, marker_id: int, marker_type: int):
        marker = self._Marker()
        marker.header.frame_id = self.fixed_frame
        marker.header.stamp = self.node.get_clock().now().to_msg()
        marker.ns = "codrive_audio"
        marker.id = marker_id
        marker.type = marker_type
        marker.action = self._Marker.ADD
        marker.pose.orientation.w = 1.0
        marker.color.a = 0.95
        return marker

    @staticmethod
    def _set_color(marker, color: tuple[float, float, float], alpha: float = 1.0) -> None:
        marker.color.r, marker.color.g, marker.color.b = color
        marker.color.a = alpha

    def _cube(
        self,
        marker_id: int,
        *,
        x: float,
        y: float,
        width: float,
        height: float,
        color: tuple[float, float, float],
        z: float = 0.02,
        alpha: float = 1.0,
    ):
        marker = self._marker(marker_id, self._Marker.CUBE)
        marker.pose.position.x = x
        marker.pose.position.y = y
        marker.pose.position.z = z
        marker.scale.x = width
        marker.scale.y = height
        marker.scale.z = 0.025
        self._set_color(marker, color, alpha)
        return marker

    def _text(
        self,
        marker_id: int,
        value: str,
        *,
        x: float,
        y: float,
        size: float,
        color: tuple[float, float, float] | None = None,
        z: float = 0.14,
    ):
        marker = self._marker(marker_id, self._Marker.TEXT_VIEW_FACING)
        marker.pose.position.x = x
        marker.pose.position.y = y
        marker.pose.position.z = z
        marker.scale.z = size
        self._set_color(marker, color or self._TEXT)
        marker.text = value
        return marker

    @staticmethod
    def _card_status(
        event_kind: AudioKind,
        card_kind: AudioKind,
        probability: float,
        threshold: float,
        enabled: bool,
    ) -> str:
        if not enabled:
            return "DISABLED / MONITOR ONLY"
        if event_kind is card_kind:
            return "DETECTED"
        if probability >= threshold:
            return "VERIFYING"
        return "MONITORING"

    def _probability_card(
        self,
        *,
        base_id: int,
        center_x: float,
        title: str,
        probability: float,
        threshold: float,
        color: tuple[float, float, float],
        card_kind: AudioKind,
        event_kind: AudioKind,
        enabled: bool,
    ) -> list[Any]:
        active = event_kind is card_kind
        panel_color = tuple(
            min(1.0, self._PANEL[index] + (color[index] * 0.10 if active else 0.0))
            for index in range(3)
        )
        center_y = 0.62
        track_width = 4.25
        track_left = center_x - track_width / 2.0
        fill_width = max(0.025, track_width * max(0.0, min(1.0, probability)))
        threshold_x = track_left + track_width * threshold
        status = self._card_status(
            event_kind, card_kind, probability, threshold, enabled
        )
        status_color = color if status in {"DETECTED", "VERIFYING"} else self._MUTED
        if not enabled:
            fill_color = self._MUTED
        else:
            fill_color = color
        return [
            self._cube(
                base_id,
                x=center_x,
                y=center_y,
                width=5.20,
                height=2.72,
                color=panel_color,
            ),
            self._cube(
                base_id + 1,
                x=center_x,
                y=1.94,
                width=5.20,
                height=0.08,
                color=color,
                z=0.05,
                alpha=1.0 if active else 0.55,
            ),
            self._text(
                base_id + 2,
                title,
                x=center_x,
                y=1.55,
                size=0.25,
                color=color if enabled else self._MUTED,
            ),
            self._text(
                base_id + 3,
                f"{probability * 100.0:04.1f}%",
                x=center_x,
                y=0.78,
                size=0.62,
                color=fill_color,
            ),
            self._text(
                base_id + 4,
                status,
                x=center_x - 1.30,
                y=0.18,
                size=0.15,
                color=status_color,
            ),
            self._text(
                base_id + 8,
                f"THRESHOLD {threshold * 100.0:.0f}%",
                x=center_x + 1.30,
                y=0.18,
                size=0.15,
                color=self._MUTED,
            ),
            self._cube(
                base_id + 5,
                x=center_x,
                y=-0.38,
                width=track_width,
                height=0.22,
                color=self._TRACK,
                z=0.06,
            ),
            self._cube(
                base_id + 6,
                x=track_left + fill_width / 2.0,
                y=-0.38,
                width=fill_width,
                height=0.22,
                color=fill_color,
                z=0.09,
                alpha=1.0 if enabled else 0.55,
            ),
            self._cube(
                base_id + 7,
                x=threshold_x,
                y=-0.38,
                width=0.035,
                height=0.38,
                color=self._TEXT,
                z=0.12,
                alpha=0.95,
            ),
        ]

    def _metric_card(
        self,
        *,
        base_id: int,
        center_x: float,
        label: str,
        value: str,
        value_color: tuple[float, float, float] | None = None,
    ) -> list[Any]:
        return [
            self._cube(
                base_id,
                x=center_x,
                y=-2.94,
                width=3.42,
                height=1.08,
                color=self._PANEL,
            ),
            self._text(
                base_id + 1,
                label,
                x=center_x,
                y=-2.67,
                size=0.14,
                color=self._MUTED,
            ),
            self._text(
                base_id + 2,
                value,
                x=center_x,
                y=-3.08,
                size=0.31,
                color=value_color,
            ),
        ]

    def _waveform_panel(self, waveform: np.ndarray) -> list[Any]:
        normalized_x, amplitude = waveform_envelope(waveform)
        _, peak, dbfs = waveform_levels(waveform)
        if peak >= 0.98:
            wave_color = self._SIREN
            level_text = "CLIPPING"
            level_color = self._SIREN
        elif peak < 0.01:
            wave_color = self._MUTED
            level_text = "LOW INPUT"
            level_color = self._HORN
        else:
            wave_color = self._WAVE
            level_text = "LEVEL OK"
            level_color = self._CLEAR

        line = self._marker(407, self._Marker.LINE_STRIP)
        line.scale.x = 0.025
        self._set_color(line, wave_color)
        for x_value, y_value in zip(normalized_x, amplitude):
            point = self._Point()
            point.x = -5.10 + float(x_value) * 10.20
            point.y = -1.55 + float(y_value) * 0.37
            point.z = 0.16
            line.points.append(point)

        return [
            self._cube(
                400,
                x=0.0,
                y=-1.55,
                width=11.05,
                height=1.18,
                color=self._PANEL,
            ),
            self._text(
                401,
                "LIVE WAVEFORM",
                x=-4.45,
                y=-1.12,
                size=0.15,
                color=self._WAVE,
            ),
            self._text(
                402,
                "960 ms",
                x=-2.95,
                y=-1.12,
                size=0.13,
                color=self._MUTED,
            ),
            self._text(
                403,
                f"RMS {dbfs:.1f} dBFS",
                x=2.15,
                y=-1.12,
                size=0.105,
                color=self._MUTED,
            ),
            self._text(
                404,
                f"PEAK {peak * 100.0:.0f}%",
                x=4.02,
                y=-1.12,
                size=0.105,
                color=self._MUTED,
            ),
            self._text(
                405,
                level_text,
                x=5.10,
                y=-1.12,
                size=0.105,
                color=level_color,
            ),
            self._cube(
                406,
                x=0.0,
                y=-1.55,
                width=10.20,
                height=0.012,
                color=self._MUTED,
                z=0.09,
                alpha=0.45,
            ),
            line,
        ]

    def publish(self, result: AudioInferenceResult) -> None:
        event = result.event
        status_color = self._color(event.kind)
        status_text = {
            AudioKind.NONE: "LISTENING / NO ALERT",
            AudioKind.SIREN: "SIREN DETECTED",
            AudioKind.HORN: "HORN DETECTED",
        }[event.kind]

        markers: list[Any] = []
        if not self._dashboard_initialized:
            clear = self._marker(0, self._Marker.CUBE)
            clear.action = self._Marker.DELETEALL
            markers.append(clear)
            self._dashboard_initialized = True
        markers.extend(
            [
                self._cube(
                    100,
                    x=0.0,
                    y=-0.43,
                    width=12.0,
                    height=7.55,
                    color=self._BACKGROUND,
                    z=0.0,
                ),
                self._cube(
                    101,
                    x=-5.67,
                    y=2.72,
                    width=0.07,
                    height=0.68,
                    color=self._CLEAR,
                    z=0.05,
                ),
                self._text(
                    102,
                    "CO-DRIVE  AUDIO MONITOR",
                    x=-3.56,
                    y=2.88,
                    size=0.28,
                ),
                self._text(
                    103,
                    "LIVE",
                    x=-5.02,
                    y=2.47,
                    size=0.14,
                    color=self._MUTED,
                ),
                self._text(
                    106,
                    "16 kHz MONO",
                    x=-3.70,
                    y=2.47,
                    size=0.14,
                    color=self._MUTED,
                ),
                self._text(
                    107,
                    event.model_version,
                    x=-2.12,
                    y=2.47,
                    size=0.14,
                    color=self._MUTED,
                ),
                self._cube(
                    104,
                    x=4.02,
                    y=2.72,
                    width=3.35,
                    height=0.72,
                    color=status_color,
                    z=0.05,
                    alpha=0.20 if event.kind is AudioKind.NONE else 0.32,
                ),
                self._text(
                    105,
                    status_text,
                    x=4.02,
                    y=2.72,
                    size=0.25,
                    color=status_color,
                ),
            ]
        )
        markers.extend(
            self._probability_card(
                base_id=200,
                center_x=-2.78,
                title="SIREN  /  EMERGENCY",
                probability=result.siren_probability,
                threshold=self.siren_threshold,
                color=self._SIREN,
                card_kind=AudioKind.SIREN,
                event_kind=event.kind,
                enabled=True,
            )
        )
        markers.extend(
            self._probability_card(
                base_id=220,
                center_x=2.78,
                title="HORN  /  VEHICLE",
                probability=result.horn_probability,
                threshold=self.horn_threshold,
                color=self._HORN,
                card_kind=AudioKind.HORN,
                event_kind=event.kind,
                enabled=self.horn_enabled,
            )
        )
        markers.extend(self._waveform_panel(result.waveform))
        confidence_label = (
            "CLEAR CONFIDENCE" if event.kind is AudioKind.NONE else "EVENT CONFIDENCE"
        )
        markers.extend(
            self._metric_card(
                base_id=300,
                center_x=-3.78,
                label=confidence_label,
                value=f"{event.confidence * 100.0:.1f}%",
                value_color=status_color,
            )
        )
        markers.extend(
            self._metric_card(
                base_id=310,
                center_x=0.0,
                label="MODEL INFERENCE",
                value=f"{result.inference_ms:.2f} ms",
            )
        )
        markers.extend(
            self._metric_card(
                base_id=320,
                center_x=3.78,
                label="WINDOW / UPDATE",
                value=f"{event.window_ms} / 100 ms",
            )
        )
        markers.extend(
            (
                self._text(
                    330,
                    "EMA + HYSTERESIS",
                    x=-3.78,
                    y=-3.76,
                    size=0.12,
                    color=self._MUTED,
                ),
                self._text(
                    331,
                    f"DIRECTION  {event.direction.upper()}",
                    x=0.0,
                    y=-3.76,
                    size=0.12,
                    color=self._MUTED,
                ),
                self._text(
                    332,
                    "Ctrl+C  STOP",
                    x=3.78,
                    y=-3.76,
                    size=0.12,
                    color=self._MUTED,
                ),
            )
        )

        marker_array = self._MarkerArray()
        marker_array.markers = markers
        self.marker_publisher.publish(marker_array)

        image_values = np.ascontiguousarray(feature_to_mono8(result.feature))
        image = self._Image()
        image.header.stamp = markers[-1].header.stamp
        image.header.frame_id = self.fixed_frame
        image.height, image.width = image_values.shape
        image.encoding = "mono8"
        image.is_bigendian = False
        image.step = image.width
        image.data = image_values.tobytes()
        self.image_publisher.publish(image)

        probabilities = self._Float32MultiArray()
        probabilities.data = [
            float(result.siren_probability),
            float(result.horn_probability),
            float(event.confidence),
        ]
        self.probability_publisher.publish(probabilities)
        self._rclpy.spin_once(self.node, timeout_sec=0.0)

    def close(self) -> None:
        self.node.destroy_node()
        if self._owns_context and self._rclpy.ok():
            self._rclpy.shutdown()

    def __enter__(self) -> "RosAudioVisualizer":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        self.close()
        return False


__all__ = ["RosAudioVisualizer", "waveform_envelope", "waveform_levels"]
