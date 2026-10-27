"""Metric ground-plane projection in the calibrated, rectified image coordinate system.

Ground coordinates are lateral x (right positive), forward z, metres relative to ego.
Four measured ground correspondences encode camera pose/scale and, for screen
recapture, the screen-to-virtual-image transform. Lens-only calibration is insufficient.
"""

from dataclasses import replace
from collections import deque
import json
from pathlib import Path
import cv2
import numpy as np
from .lane.association import Boundary, LanePairTracker


class GroundGeometry:
    def __init__(self, config):
        self.size = np.asarray(config["image_size"], dtype=float)
        self.pixels = np.asarray(config["image_points"], dtype=np.float32)
        ground = np.asarray(config["ground_points_m"], dtype=np.float32)
        if (
            self.size.shape != (2,)
            or np.any(self.size <= 0)
            or self.pixels.shape != (4, 2)
            or ground.shape != (4, 2)
            or not np.isfinite(self.pixels).all()
            or not np.isfinite(ground).all()
            or not cv2.isContourConvex(self.pixels)
            or not cv2.isContourConvex(ground)
        ):
            raise ValueError(
                "Four convex, ordered image/metric ground correspondences required"
            )
        self.h = cv2.getPerspectiveTransform(self.pixels, ground)
        if not np.isfinite(self.h).all() or abs(np.linalg.det(self.h)) < 1e-12:
            raise ValueError("Degenerate ground calibration")
        self.maximum = float(config.get("maximum_distance_m", 80))
        self.half_width = float(config.get("path_half_width_m", 1.5))
        if not 0 < self.maximum <= 200 or not 0 < self.half_width <= 5:
            raise ValueError("Invalid ground bounds")
        self.tracks = {}
        self.next_id = 1
        self.last_time = None
        self.lane_tracker = LanePairTracker(config.get("vehicle_motion"))
        self.selected_slots = []
        self.lane_status = self.lane_tracker.status

    @classmethod
    def load(cls, path):
        return cls(json.loads(Path(path).read_text()))

    def project(self, x, y):
        p = np.array([x * self.size[0], y * self.size[1]], np.float32)
        # No extrapolation beyond the region with measured metric reference points.
        if cv2.pointPolygonTest(self.pixels, tuple(map(float, p)), False) < 0:
            return None
        q = self.h @ np.array([*p, 1.0])
        if abs(q[2]) < 1e-9:
            return None
        x, z = q[:2] / q[2]
        if not np.isfinite([x, z]).all() or z <= 0 or np.hypot(x, z) > self.maximum:
            return None
        return float(x), float(z)

    @staticmethod
    def iou(a, b):
        inter = max(0, min(a.x2, b.x2) - max(a.x1, b.x1)) * max(
            0, min(a.y2, b.y2) - max(a.y1, b.y1)
        )
        union = (a.x2 - a.x1) * (a.y2 - a.y1) + (b.x2 - b.x1) * (b.y2 - b.y1) - inter
        return inter / union if union > 0 else 0

    def enrich(self, perception, lanes=()):
        stamp = perception.header.capture_time_ns
        if self.last_time is not None and not 0 < stamp - self.last_time <= 750_000_000:
            self.tracks.clear()
        self.last_time = stamp
        old = self.tracks
        fresh = {}
        objects = []
        used = set()
        for obj in perception.objects:
            if obj.class_name not in {
                "person",
                "pedestrian",
                "car",
                "vehicle",
                "truck",
                "bus",
                "bicycle",
                "motorcycle",
            }:
                objects.append(obj)
                continue
            point = self.project((obj.bbox.x1 + obj.bbox.x2) / 2, obj.bbox.y2)
            if point is None:
                objects.append(replace(obj, distance_m=None, ttc_s=None, in_path=None))
                continue
            matches = [
                (self.iou(obj.bbox, v[0].bbox), k)
                for k, v in old.items()
                if k not in used and v[0].class_name == obj.class_name
            ]
            overlap, ident = max(matches, default=(0, None))
            if overlap < 0.3:
                # A crossing pedestrian can move farther than its narrow BBOX
                # width between 6 FPS frames. Use bounded metric association
                # rather than resetting its velocity track on every step.
                candidates=[]
                for key,(previous,history) in old.items():
                    if key in used or previous.class_name != obj.class_name or not history:continue
                    dt=(stamp-history[-1][0])/1e9
                    if not 0 < dt <= .5:continue
                    before=self.project((previous.bbox.x1+previous.bbox.x2)/2,previous.bbox.y2)
                    if before is None:continue
                    px,pz=before
                    motion=self.lane_tracker.motion
                    if motion and abs(stamp-motion[2])<=500_000_000:
                        speed,curvature,_=motion;distance=speed*dt;yaw=distance*curvature
                        mx=(1-np.cos(yaw))/curvature if abs(curvature)>1e-7 else 0.
                        mz=np.sin(yaw)/curvature if abs(curvature)>1e-7 else distance
                        dx,dz=px-mx,pz-mz
                        px,pz=dx*np.cos(yaw)-dz*np.sin(yaw),dx*np.sin(yaw)+dz*np.cos(yaw)
                    lateral=max(.5,4*dt);longitudinal=max(1.,6*dt)
                    error=((point[0]-px)/lateral)**2+((point[1]-pz)/longitudinal)**2
                    if error<=1:candidates.append((error,key))
                if candidates:ident=min(candidates)[1]
                else:
                    ident = self.next_id
                    self.next_id += 1
            used.add(ident)
            history = deque(old[ident][1]) if ident in old else deque()
            x, z = point
            history.append((stamp, z))
            while len(history) > 1 and stamp - history[0][0] > 500_000_000:
                history.popleft()
            ttc = None
            if (
                len(history) >= 3
                and stamp - history[0][0] >= 200_000_000
                and abs(x) <= self.half_width
            ):
                times = np.array([(t - stamp) / 1e9 for t, _ in history])
                zs = np.array([v for _, v in history])
                closing = -float(
                    np.dot(times - times.mean(), zs - zs.mean())
                    / np.sum((times - times.mean()) ** 2)
                )
                if 0.1 < closing <= 80:
                    ttc = z / closing
            updated = replace(
                obj,
                track_id=ident,
                distance_m=float(np.hypot(x, z)),
                ttc_s=ttc,
                in_path=abs(x) <= self.half_width,
            )
            objects.append(updated)
            fresh[ident] = (updated, history)
        self.tracks = fresh
        boundaries=[]
        for index,lane in enumerate(lanes):
            key=getattr(lane,"slot_id",index)
            if key < 0:key=index
            if getattr(lane,"confidence",0) < .5:continue
            if hasattr(lane,"points"):
                points=[self.project(x,y) for x,y in lane.points]
                points=np.asarray([q for q in points if q is not None and 4<=q[1]<=16])
                if len(points)<8 or np.ptp(points[:,1])<3:continue
                coefficients=np.polyfit(points[:,1],points[:,0],2)
                residual=np.sqrt(np.mean((points[:,0]-np.polyval(coefficients,points[:,1]))**2))
                if residual>.20 or abs(coefficients[0])>.08:continue
            elif all(hasattr(lane,n) for n in ("x_bottom","y_bottom","x_top","y_top")):
                near=self.project(lane.x_bottom,lane.y_bottom)
                far=self.project(lane.x_top,lane.y_top)
                if near is None or far is None or abs(far[1]-near[1])<=2:continue
                slope=(far[0]-near[0])/(far[1]-near[1])
                coefficients=(0.,slope,near[0]-slope*near[1])
            else:continue
            boundaries.append(Boundary(key,tuple(coefficients),lane.confidence))
        selected=self.lane_tracker.update(boundaries,stamp)
        self.lane_status=self.lane_tracker.status
        self.selected_slots=self.lane_status['selected_slots']
        offset,confidence,heading=selected if selected is not None else (None,0.,None)
        return replace(
            perception,
            objects=tuple(objects),
            lane_offset_m=offset,
            lane_confidence=confidence,
            lane_heading_error_deg=heading,
        )
