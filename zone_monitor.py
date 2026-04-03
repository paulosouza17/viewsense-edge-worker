import time
import logging
import numpy as np
import supervision as sv
from typing import List, Dict, Any, Tuple, Optional
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

@dataclass
class MonitoredZone:
    """Represents a zone for dwell time monitoring."""
    roi_id: str
    name: str
    polygon: np.ndarray  # Shape (N, 2) in pixel coords
    min_dwell_time: float = 1.0

    # Internal State: {track_id: enter_time}
    current_objects: Dict[str, float] = field(default_factory=dict)

    # Supervision Zone
    _sv_zone: sv.PolygonZone = field(init=False, repr=False)

    def __post_init__(self):
        self._sv_zone = sv.PolygonZone(
            polygon=self.polygon,
            triggering_anchors=(sv.Position.BOTTOM_CENTER,)
        )


class ZoneMonitor:
    """
    Manages multiple dwell-time zones and tracks how long objects stay inside.
    """
    def __init__(self):
        self.zones: List[MonitoredZone] = []

    def rebuild_zones(self, rois: List[Dict[str, Any]], width: int, height: int):
        """
        Rebuild zones from ROI configs (synced from backend).
        Only includes ROIs where is_dwell_zone=True and roi_type is rectangle or polygon.
        Coordinates are expected normalized (0-1) and converted to pixel coords.
        """
        self.zones.clear()

        for roi in rois:
            if not roi.get("is_dwell_zone"):
                continue
            if roi.get("roi_type") not in ("rectangle", "polygon"):
                continue
            if not roi.get("is_active", True):
                continue

            coords = roi.get("coordinates", [])
            if len(coords) < 3:
                continue

            # Convert normalized coords to pixel coords
            points = []
            for pt in coords:
                px = pt.get("x", 0)
                py = pt.get("y", 0)
                # Detect if already in pixels or normalized
                if px <= 1.0 and py <= 1.0:
                    px = px * width
                    py = py * height
                points.append([int(px), int(py)])

            poly = np.array(points, dtype=np.int32)
            zone = MonitoredZone(
                roi_id=roi["id"],
                name=roi.get("name", "Zone"),
                polygon=poly,
            )
            self.zones.append(zone)
            logger.info(f"🟦 Dwell zone '{zone.name}' added ({len(points)} pts, roi={zone.roi_id})")

        logger.info(f"ZoneMonitor: {len(self.zones)} dwell zones active")

    _frame_count: int = 0

    def update(self, detections: sv.Detections) -> List[Dict[str, Any]]:
        """
        Update zones with new detections and return dwell-time events.
        Returns list of dicts:
          {type, roi_id, track_id, dwell_time, detection_class, confidence}
        """
        events = []
        current_time = time.time()
        self._frame_count += 1

        if detections.tracker_id is None or len(self.zones) == 0:
            return []

        for zone in self.zones:
            is_in_zone = zone._sv_zone.trigger(detections=detections)
            ids_in_zone = detections.tracker_id[is_in_zone]
            str_ids_in_zone = set(str(tid) for tid in ids_in_zone)

            # Debug: periodic logging
            if self._frame_count % 50 == 0:
                n_total = len(detections.tracker_id)
                n_inside = int(is_in_zone.sum())
                n_tracked = len(zone.current_objects)
                zmin = zone.polygon.min(axis=0).tolist()
                zmax = zone.polygon.max(axis=0).tolist()
                # Show a few bottom-center positions
                if detections.xyxy is not None and len(detections.xyxy) > 0:
                    bcs = []
                    for box in detections.xyxy[:3]:
                        bcx = (box[0] + box[2]) / 2
                        bcy = box[3]  # bottom
                        bcs.append(f"({bcx:.0f},{bcy:.0f})")
                    bc_str = " ".join(bcs)
                else:
                    bc_str = "no-boxes"
                logger.info(
                    f"🔍 Zone '{zone.name}': {n_inside}/{n_total} in zone, "
                    f"{n_tracked} tracked | zone=[{zmin}→{zmax}] | bc=[{bc_str}]"
                )

            # Build lookup for detection info
            det_info = {}
            for i, tid in enumerate(detections.tracker_id):
                det_info[str(tid)] = {
                    "class_id": int(detections.class_id[i]) if detections.class_id is not None else 0,
                    "confidence": float(detections.confidence[i]) if detections.confidence is not None else 0.0,
                }

            # Handle new entries
            for track_id in str_ids_in_zone:
                if track_id not in zone.current_objects:
                    zone.current_objects[track_id] = current_time

            # Handle exits
            for track_id, start_time in list(zone.current_objects.items()):
                if track_id not in str_ids_in_zone:
                    duration = current_time - start_time
                    if duration >= zone.min_dwell_time:
                        info = det_info.get(track_id, {})
                        events.append({
                            "type": "zone_dwell",
                            "roi_id": zone.roi_id,
                            "track_id": track_id,
                            "dwell_time": round(duration, 2),
                            "class_id": info.get("class_id", 0),
                            "confidence": info.get("confidence", 0.0),
                        })
                        logger.info(
                            f"⏱️ Dwell: track {track_id} stayed {duration:.1f}s in '{zone.name}'"
                        )
                    del zone.current_objects[track_id]

        return events
