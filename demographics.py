"""
demographics.py — Gender & Age estimation from person crops.
Uses InsightFace (buffalo_sc model) for lightweight face analysis.
Only outputs statistics — NO images are ever stored.
"""
import logging
import time
import threading
from typing import Dict, Optional, Tuple

import cv2
import numpy as np

logger = logging.getLogger(__name__)

# Age bucket boundaries matching frontend DemographicsChart
AGE_BUCKETS = [
    (0, 17, "0-17"),
    (18, 24, "18-24"),
    (25, 34, "25-34"),
    (35, 44, "35-44"),
    (45, 54, "45-54"),
    (55, 64, "55-64"),
]


def _age_to_bucket(age: int) -> str:
    """Convert raw age estimate to age bucket string."""
    for lo, hi, label in AGE_BUCKETS:
        if lo <= age <= hi:
            return label
    return "65+"


class DemographicsAnalyzer:
    """
    Analyzes person crops for gender and approximate age.
    Thread-safe, lazy-loads model on first use.
    Processes ONLY persons (class 0 in COCO).
    Never stores face images — results are statistical only.
    """

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self._model = None
        self._lock = threading.Lock()
        self._loaded = False
        self._load_failed = False

        # Cache: track_id -> demographics (avoid re-analyzing same person)
        self._cache: Dict[str, Dict] = {}
        self._cache_max = 500
        self._last_cleanup = time.time()

        # Rate limiting: analyze at most every N frames per track
        self._min_interval_seconds = 2.0

        # Stats
        self.total_analyzed = 0
        self.total_faces_found = 0

    def _load_model(self):
        """Lazy-load InsightFace model on first use."""
        if self._load_failed:
            return False

        try:
            from insightface.app import FaceAnalysis

            logger.info("📊 Loading demographics model (InsightFace buffalo_sc)...")
            start = time.time()

            # buffalo_sc = smallest model, CPU-friendly
            self._model = FaceAnalysis(
                name="buffalo_sc",
                allowed_modules=["detection", "genderage"],
                providers=["CPUExecutionProvider"],
            )
            # Small detection size for speed (faces in person crops are usually large)
            self._model.prepare(ctx_id=-1, det_size=(160, 160))

            elapsed = time.time() - start
            logger.info(f"✅ Demographics model loaded in {elapsed:.1f}s")
            self._loaded = True
            return True

        except ImportError:
            logger.warning(
                "⚠️ InsightFace not installed. Demographics disabled. "
                "Install with: pip install insightface onnxruntime"
            )
            self._load_failed = True
            self.enabled = False
            return False
        except Exception as e:
            logger.error(f"❌ Failed to load demographics model: {e}")
            self._load_failed = True
            self.enabled = False
            return False

    def analyze(
        self, frame: np.ndarray, track_id: str, bbox: Dict[str, int]
    ) -> Optional[Dict]:
        """
        Analyze a detected person for gender and age.

        Args:
            frame: Full video frame (BGR, np.ndarray)
            track_id: Unique tracking ID for this person
            bbox: Bounding box {"x", "y", "width", "height"}

        Returns:
            Dict with "gender", "age", "age_raw", "confidence" or None
        """
        if not self.enabled:
            return None

        # Check cache — don't re-analyze same person within interval
        cached = self._cache.get(track_id)
        if cached and (time.time() - cached["_ts"]) < self._min_interval_seconds:
            return {k: v for k, v in cached.items() if not k.startswith("_")}

        with self._lock:
            if not self._loaded:
                if not self._load_model():
                    return None

        try:
            # Crop person from frame with padding
            h, w = frame.shape[:2]
            x1 = max(0, bbox["x"])
            y1 = max(0, bbox["y"])
            x2 = min(w, bbox["x"] + bbox["width"])
            y2 = min(h, bbox["y"] + bbox["height"])

            # Focus on upper body (face is typically in top 40% of person bbox)
            body_h = y2 - y1
            face_y2 = min(y2, y1 + int(body_h * 0.5))

            crop = frame[y1:face_y2, x1:x2]
            if crop.size == 0 or crop.shape[0] < 20 or crop.shape[1] < 20:
                return None

            # Run face detection + gender/age classification
            self.total_analyzed += 1
            faces = self._model.get(crop)

            if not faces:
                return None

            self.total_faces_found += 1

            # Use the largest (most confident) face
            face = max(faces, key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]))

            gender_raw = int(face.gender) if hasattr(face, "gender") else -1
            age_raw = int(face.age) if hasattr(face, "age") else -1

            if gender_raw < 0 or age_raw < 0:
                return None

            result = {
                "gender": "male" if gender_raw == 1 else "female",
                "age": _age_to_bucket(age_raw),
                "age_raw": age_raw,
            }

            # Cache result
            self._cache[track_id] = {**result, "_ts": time.time()}
            self._cleanup_cache()

            return result

        except Exception as e:
            logger.debug(f"Demographics analysis error for track {track_id}: {e}")
            return None

    def _cleanup_cache(self):
        """Remove stale entries from cache."""
        now = time.time()
        if now - self._last_cleanup < 30:
            return

        self._last_cleanup = now
        stale = [
            k for k, v in self._cache.items()
            if now - v["_ts"] > 60
        ]
        for k in stale:
            del self._cache[k]

        # Hard cap
        if len(self._cache) > self._cache_max:
            oldest = sorted(self._cache.items(), key=lambda x: x[1]["_ts"])
            for k, _ in oldest[: len(self._cache) - self._cache_max]:
                del self._cache[k]

    def get_stats(self) -> Dict:
        """Return diagnostic stats."""
        return {
            "enabled": self.enabled,
            "loaded": self._loaded,
            "total_analyzed": self.total_analyzed,
            "total_faces_found": self.total_faces_found,
            "cache_size": len(self._cache),
            "face_rate": (
                f"{self.total_faces_found / self.total_analyzed * 100:.0f}%"
                if self.total_analyzed > 0
                else "N/A"
            ),
        }
