"""
detector.py — Mac-adapted Camera Detector
Uses YOLOv8 with CPU inference, ByteTrack tracking, and line crossing detection.
HLS streams are captured via subprocess ffmpeg (Mac ARM OpenCV lacks HLS support).
"""
import time
import logging
import subprocess
import cv2
import threading
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime
import supervision as sv
from ultralytics import YOLO
import numpy as np
import asyncio
import os

from api_client import APIClient
from line_crossing import LineCrossingDetector, CountingLine
from zone_monitor import ZoneMonitor
from demographics import DemographicsAnalyzer

logger = logging.getLogger(__name__)


class CameraDetector(threading.Thread):
    def __init__(self, camera_config: Dict[str, Any], api_client: APIClient):
        super().__init__(daemon=True)
        self.config = camera_config
        self.api_client = api_client
        self.running = False
        self.camera_id = self.config['id']

        raw_url = self.config.get('stream_url', '')
        if isinstance(raw_url, str):
            raw_url = raw_url.strip()

            if raw_url.startswith("rtmp://"):
                # Always rewrite the host to 127.0.0.1 so each server reads
                # from its own local RTMP server (port 55935), regardless of
                # which IP is stored in the database.
                import re as _re
                # Extract just the /live/{hash} path
                match = _re.search(r'(rtmp://)[^/]+(/.+)', raw_url)
                if match:
                    path = match.group(2)
                    # Normalize port: replace :1935 with :55935 if present
                    self.source = f"rtmp://127.0.0.1:55935{path}"
                else:
                    self.source = raw_url.replace("localhost:1935", "localhost:55935")
            elif raw_url.startswith("/live/") or ("live/" in raw_url and not raw_url.startswith(("http", "rtsp://"))):
                self.source = f"rtmp://127.0.0.1:55935{raw_url if raw_url.startswith('/') else '/' + raw_url}"
            else:
                # Non-RTMP source (HLS, file, webcam) — use as-is
                self.source = raw_url
        else:
            self.source = raw_url

        if isinstance(self.source, str) and self.source.isdigit():
            self.source = int(self.source)

        self.model_name = self.config.get('model', 'yolov8n.pt')
        self.conf_threshold = self.config.get('confidence_threshold', 0.4)
        self.target_classes = self.config.get('classes', [0])
        self.inference_size = self.config.get('inference_size', 640)

        logger.info(f"Initializing CameraDetector for {self.camera_id}...")

        # Load YOLO model (.pt supports dynamic inference sizes)
        self.model = YOLO(self.model_name, task='detect')
        logger.info(f"🚀 YOLO model '{self.model_name}' loaded (inference_size={self.inference_size})")

        # ByteTrack
        fps = self.config.get('fps', 5)
        self.tracker = sv.ByteTrack(
            frame_rate=fps,
            track_activation_threshold=0.03,  # Force ultra-low tracker rejection
            minimum_matching_threshold=0.95,  # 95% distance allowed (5% overlap) for teleportation gaps
            lost_track_buffer=150
        )

        # Line Crossing
        self.crossing_detector = LineCrossingDetector()
        self.counting_lines: List[CountingLine] = []

        # Dwell Time Zones
        self.zone_monitor = ZoneMonitor()

        # Demographics (gender + age estimation)
        demographics_enabled = self.config.get('demographics_enabled', True)
        self.demographics = DemographicsAnalyzer(enabled=demographics_enabled)

        # State
        self.raw_rois: List[Dict] = []
        self.current_resolution: Optional[Tuple[int, int]] = None
        self.roi_id = self.config.get('roi_id')

        # Telemetry
        self.fps_monitor = sv.FPSMonitor()
        self._fps_val = 0.0
        self.frame_count = 0
        self.detection_count = 0

        # Stream output
        self.latest_frame: Optional[np.ndarray] = None
        self.lock = threading.Lock()

        # Annotators
        self.box_annotator = sv.BoxAnnotator()
        self.trace_annotator = sv.TraceAnnotator()
        try:
            self.label_annotator = sv.LabelAnnotator()
        except AttributeError:
            self.label_annotator = None

    # ----- Stream helpers -----

    def _is_network_stream(self) -> bool:
        return isinstance(self.source, str) and (
            self.source.startswith('http') or self.source.startswith('rtsp') or self.source.startswith('rtmp')
        )

    def _probe_resolution(self) -> Tuple[int, int]:
        try:
            result = subprocess.run([
                'ffprobe', '-v', 'error',
                '-select_streams', 'v:0',
                '-show_entries', 'stream=width,height',
                '-of', 'csv=p=0:s=x',
                self.source
            ], capture_output=True, text=True, timeout=15)
            parts = result.stdout.strip().split('x')
            if len(parts) == 2:
                return int(parts[0]), int(parts[1])
        except Exception as e:
            logger.warning(f"ffprobe failed: {e}")
        return 1280, 720

    def _start_ffmpeg(self, width: int, height: int):
        target_fps = self.config.get('fps', 5)
        is_rtmp = isinstance(self.source, str) and self.source.startswith('rtmp://')

        if is_rtmp:
            # RTMP: -reconnect flags are HTTP-only and break RTMP connections
            cmd = [
                'ffmpeg',
                '-loglevel', 'warning',
                '-rtmp_live', 'live',
                '-i', str(self.source),
                '-map', '0:v:0',
                '-an',
                '-s', f'{width}x{height}',
                '-r', str(target_fps),
                '-f', 'rawvideo',
                '-pix_fmt', 'bgr24',
                'pipe:1'
            ]
        else:
            cmd = [
                'ffmpeg',
                '-loglevel', 'warning',
                '-analyzeduration', '10000000',
                '-probesize', '10000000',
                '-reconnect', '1',
                '-reconnect_streamed', '1',
                '-reconnect_delay_max', '5',
                '-i', str(self.source),
                '-map', '0:v:0',
                '-an',
                '-s', f'{width}x{height}',
                '-r', str(target_fps),
                '-f', 'rawvideo',
                '-pix_fmt', 'bgr24',
                'pipe:1'
            ]
        frame_bytes = width * height * 3
        return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                bufsize=frame_bytes * 2)


    # ----- Main loop -----

    def run(self):
        logger.info(f"🎬 Starting detection loop for {self.config.get('name', self.camera_id)}")
        logger.info(f"   Source: {self.source}")
        self.running = True

        while self.running:
            try:
                if self._is_network_stream():
                    self._run_ffmpeg_capture()
                else:
                    self._run_cv2_capture()
            except Exception as e:
                logger.error(f"Error in camera loop: {e}")
                time.sleep(5)

    def _run_ffmpeg_capture(self):
        """Capture via subprocess ffmpeg (HLS/HTTP/RTSP)."""
        import threading
        width, height = self._probe_resolution()
        logger.info(f"📐 Stream resolution: {width}x{height}")

        proc = self._start_ffmpeg(width, height)
        logger.info(f"✅ ffmpeg started for stream capture")

        frame_size = width * height * 3
        shared_state = {"frame": None, "running": True, "error": False}

        def _reader_thread():
            try:
                while self.running and shared_state["running"]:
                    raw = b''
                    while len(raw) < frame_size:
                        chunk = proc.stdout.read(frame_size - len(raw))
                        if not chunk:
                            break
                        raw += chunk

                    if len(raw) != frame_size:
                        shared_state["error"] = True
                        break
                    
                    # Store latest frame, overwriting any old one
                    shared_state["frame"] = np.frombuffer(raw, dtype=np.uint8).reshape((height, width, 3))
            except Exception as e:
                logger.error(f"FFmpeg reader thread error: {e}")
                shared_state["error"] = True
            finally:
                shared_state["running"] = False

        thread = threading.Thread(target=_reader_thread, daemon=True)
        thread.start()

        try:
            while self.running:
                if shared_state["error"]:
                    break
                
                frame = shared_state["frame"]
                if frame is not None:
                    shared_state["frame"] = None  # Consume frame
                    self._process_frame(frame, width, height)
                else:
                    time.sleep(0.01)

        finally:
            shared_state["running"] = False
            proc.kill()
            proc.wait()
            is_mp4 = isinstance(self.source, str) and self.source.lower().split("?")[0].endswith(".mp4")
            delay = 0.1 if is_mp4 else 5
            logger.info(f"ffmpeg terminated. Reconnecting in {delay}s...")
            time.sleep(delay)

    def _run_cv2_capture(self):
        """Capture via OpenCV (local files, webcam)."""
        cap = cv2.VideoCapture(self.source)
        if not cap.isOpened():
            logger.error("Failed to open local source. Retrying in 5s...")
            time.sleep(5)
            return

        logger.info("✅ Connected to local source")
        target_fps = self.config.get('fps', 5)
        frame_interval = max(1, int(30 / target_fps))
        frame_idx = 0

        while self.running:
            ret, frame = cap.read()
            if not ret:
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                self.tracker = sv.ByteTrack(
                    frame_rate=target_fps,
                    track_activation_threshold=0.10,
                    minimum_matching_threshold=0.9
                )
                self.crossing_detector = LineCrossingDetector()
                ret, frame = cap.read()
                if not ret:
                    break

            frame_idx += 1
            if frame_idx % frame_interval != 0:
                continue

            h, w = frame.shape[:2]
            self._process_frame(frame, w, h)

        cap.release()

    # ----- Core processing -----

    def _process_frame(self, frame: np.ndarray, width: int, height: int):
        """Run YOLO + tracking + line crossing on a single frame."""
        self.frame_count += 1
        self.fps_monitor.tick()

        if self.current_resolution != (width, height):
            self._rebuild_lines(width, height)

        # YOLO inference
        results = self.model(
            frame, 
            verbose=False, 
            conf=self.conf_threshold, 
            iou=0.40, 
            imgsz=self.inference_size
        )[0]
        detections = sv.Detections.from_ultralytics(results)

        if self.target_classes:
            target_int = [int(c) for c in self.target_classes]
            detections = detections[np.isin(detections.class_id, target_int)]

        detections = self.tracker.update_with_detections(detections)

        active_track_ids = set()
        annotated = frame.copy()
        
        labels = []
        for i in range(len(detections)):
            class_id = detections.class_id[i]
            tracker_id = detections.tracker_id[i] if detections.tracker_id is not None else ""
            conf = detections.confidence[i]
            class_name = self.model.names.get(class_id, f"class_{class_id}")
            lbl = f"{class_name} {conf:.2f}"
            if tracker_id:
                lbl = f"#{tracker_id} {lbl}"
            labels.append(lbl)

        annotated = self.trace_annotator.annotate(scene=annotated, detections=detections)
        if getattr(self, "label_annotator", None):
            annotated = self.box_annotator.annotate(scene=annotated, detections=detections)
            annotated = self.label_annotator.annotate(scene=annotated, detections=detections, labels=labels)
        else:
            try:
                annotated = self.box_annotator.annotate(scene=annotated, detections=detections, labels=labels)
            except TypeError:
                annotated = self.box_annotator.annotate(scene=annotated, detections=detections)

        # Process each detection
        if detections.tracker_id is not None:
            for i, track_id in enumerate(detections.tracker_id):
                tid = str(track_id)
                active_track_ids.add(tid)

                x1, y1, x2, y2 = detections.xyxy[i]
                bbox = {"x": int(x1), "y": int(y1), "width": int(x2-x1), "height": int(y2-y1)}

                crossings = self.crossing_detector.update(track_id=tid, bbox=bbox, lines=self.counting_lines)

                class_id = int(detections.class_id[i])
                class_name = self.model.names.get(class_id, f"class_{class_id}")

                # Demographics: analyze face for gender/age (persons only)
                demo_data = None
                if class_name == "person":
                    demo_data = self.demographics.analyze(frame, tid, bbox)

                if crossings:
                    crossing = crossings[0]
                    self.detection_count += 1
                    demo_label = ""
                    if demo_data:
                        demo_label = f" [{demo_data['gender']}, {demo_data['age']}]"
                    logger.info(f"🚶 CROSSING: {class_name} #{tid} → {crossing['direction']}{demo_label}")

                    payload = {
                        "camera_id": self.camera_id,
                        "timestamp": datetime.utcnow().isoformat() + "Z",
                        "detection_class": class_name,
                        "confidence": float(detections.confidence[i]),
                        "track_id": tid,
                        "bounding_box": bbox,
                        "roi_id": crossing['roi_id'],
                        "crossed_line": True,
                        "direction": crossing['direction'],
                    }

                    # Attach demographics if available
                    if demo_data:
                        payload["gender"] = demo_data["gender"]
                        payload["age"] = demo_data["age"]

                    if self.api_client and self.api_client.loop and not self.api_client.loop.is_closed():
                        try:
                            asyncio.run_coroutine_threadsafe(
                                self.api_client.add_detection(payload),
                                self.api_client.loop
                            )
                        except Exception as e:
                            logger.error(f"API call error: {e}")

        self.crossing_detector.cleanup_stale_tracks(active_track_ids)

        # Process dwell time zones
        dwell_events = self.zone_monitor.update(detections)
        for evt in dwell_events:
            class_id = evt.get("class_id", 0)
            class_name = self.model.names.get(class_id, f"class_{class_id}")
            payload = {
                "camera_id": self.camera_id,
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "detection_class": class_name,
                "confidence": evt.get("confidence", 0.5),
                "track_id": evt["track_id"],
                "roi_id": evt["roi_id"],
                "crossed_line": False,
                "direction": None,
                "dwell_time_seconds": evt["dwell_time"],
            }

            # Attach demographics for dwell events (persons only)
            if class_name == "person":
                dwell_demo = self.demographics.analyze(
                    frame, evt["track_id"],
                    {"x": 0, "y": 0, "width": width, "height": height}  # fallback bbox
                )
                if dwell_demo:
                    payload["gender"] = dwell_demo["gender"]
                    payload["age"] = dwell_demo["age"]

            if self.api_client and self.api_client.loop and not self.api_client.loop.is_closed():
                try:
                    asyncio.run_coroutine_threadsafe(
                        self.api_client.add_detection(payload),
                        self.api_client.loop
                    )
                except Exception as e:
                    logger.error(f"Dwell API call error: {e}")

        # Draw dwell zones
        for zone in self.zone_monitor.zones:
            pts = zone.polygon.reshape((-1, 1, 2))
            cv2.polylines(annotated, [pts], True, (255, 200, 0), 2)
            count = len(zone.current_objects)
            if count > 0:
                cx = int(zone.polygon[:, 0].mean())
                cy = int(zone.polygon[:, 1].mean())
                cv2.putText(annotated, f"{zone.name}: {count}",
                            (cx - 30, cy), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 200, 0), 2)

        # Draw counting lines
        for line in self.counting_lines:
            cv2.line(annotated,
                     (int(line.p1[0]), int(line.p1[1])),
                     (int(line.p2[0]), int(line.p2[1])),
                     (0, 255, 0), 2)

        try:
            fps_val = self.fps_monitor() if callable(self.fps_monitor) else self.fps_monitor.fps
        except Exception:
            fps_val = self._fps_val
        self._fps_val = fps_val
        cv2.putText(annotated, f"FPS: {fps_val:.1f} | Det: {self.detection_count}",
                    (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)

        with self.lock:
            self.latest_frame = annotated

        # Log periodically
        if self.frame_count % 50 == 0:
            logger.info(f"📊 Frames: {self.frame_count} | FPS: {fps_val:.1f} | Crossings: {self.detection_count}")

    # ----- Settings -----

    def update_settings(self, rois: List[Dict[str, Any]], camera_config: Dict[str, Any]):
        logger.info(f"Updating settings for camera {self.camera_id}")
        if camera_config:
            new_conf = camera_config.get('confidence_threshold')
            if new_conf:
                self.conf_threshold = float(new_conf)
            new_classes = camera_config.get('enabled_classes')
            if new_classes and hasattr(self.model, 'names'):
                target_ints = []
                name_to_id = {v: k for k, v in self.model.names.items()}
                for c in new_classes:
                    if isinstance(c, int):
                        target_ints.append(c)
                    elif isinstance(c, str) and c in name_to_id:
                        target_ints.append(name_to_id[c])
                self.target_classes = target_ints

            # Live toggle of demographics (age/gender estimation)
            if 'demographics_enabled' in camera_config:
                demo_enabled = bool(camera_config['demographics_enabled'])
                if self.demographics.enabled != demo_enabled:
                    self.demographics.enabled = demo_enabled
                    logger.info(
                        f"Camera {self.camera_id[:8]}: demographics {'enabled' if demo_enabled else 'disabled'}"
                    )

        with self.lock:
            self.raw_rois = rois
            self.current_resolution = None

    def _rebuild_lines(self, width: int, height: int):
        if not self.raw_rois:
            self.counting_lines = []
            self.zone_monitor.rebuild_zones([], width, height)
            self.current_resolution = (width, height)
            return
        logger.info(f"Rebuilding counting lines & dwell zones for {width}x{height}...")
        try:
            new_lines = [CountingLine.from_roi(r, width, height) for r in self.raw_rois]
            self.counting_lines = [l for l in new_lines if l]
            self.current_resolution = (width, height)
            logger.info(f"Active counting lines: {len(self.counting_lines)}")
        except Exception as e:
            logger.error(f"Failed to rebuild lines: {e}")
        try:
            self.zone_monitor.rebuild_zones(self.raw_rois, width, height)
        except Exception as e:
            logger.error(f"Failed to rebuild dwell zones: {e}")

    def stop(self):
        self.running = False

    def get_latest_frame(self):
        with self.lock:
            return self.latest_frame.copy() if self.latest_frame is not None else None

    def get_status(self):
        return {
            "id": self.camera_id,
            "name": self.config.get('name', self.camera_id),
            "fps": self._fps_val,
            "frames_processed": self.frame_count,
            "detections_sent": self.detection_count,
            "running": self.running,
            "resolution": self.current_resolution,
            "active_lines": len(self.counting_lines),
            "demographics": self.demographics.get_stats(),
        }
