from typing import List, Dict, Any, Optional
import numpy as np
from ultralytics import YOLO
import supervision as sv
import cv2

class Tracker:
    def __init__(self, model_path: str = "yolov8n.pt", frame_rate: int = 30):
        # Initialize ByteTrack
        # frame_rate is used by ByteTrack for internal buffer management
        self.tracker = sv.ByteTrack(frame_rate=frame_rate)
        
    def update(self, detections: sv.Detections) -> sv.Detections:
        """
        Updates the tracker with new detections.
        Returns the detections with assigned tracker_ids.
        """
        return self.tracker.update_with_detections(detections)

# Note: The 'detector.py' will handle the YOLO inference and passing Detections to this Tracker.
