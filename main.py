"""
ViewSense Edge Worker — Mac Version
main.py — FastAPI server with YOLO detection on HLS/RTSP streams.

Usage:
    python main.py
    # Server runs on http://localhost:8765
    # MJPEG stream: http://localhost:8765/video/<camera_id>
    # Status:       http://localhost:8765/status
"""
import os
os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "timeout;10000"

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import logging
import asyncio
import time
import cv2

from camera_manager import CameraManager

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s - %(message)s'
)
logger = logging.getLogger("viewsense-mac")

camera_manager = CameraManager("config.yaml")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 ViewSense Edge Worker (Mac) starting...")
    loop = asyncio.get_running_loop()
    camera_manager.api_client_loop = loop
    camera_manager.start()

    if camera_manager.api_client:
        asyncio.create_task(camera_manager.api_client.heartbeat_loop())
        asyncio.create_task(camera_manager.api_client.flush_loop())

    logger.info("✅ All cameras started")
    yield
    logger.info("🛑 Shutting down...")
    await camera_manager.shutdown()


app = FastAPI(title="ViewSense Edge Worker (Mac)", lifespan=lifespan)

# CORS — allow frontend at localhost:8080 to call this API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/status")
async def status():
    stats = [det.get_status() for det in camera_manager.cameras.values()]
    sync_status = camera_manager.config_sync.get_status() if camera_manager.config_sync else None
    return {
        "version": "1.1.0-mac",
        "cameras": stats,
        "total_cameras": len(stats),
        "config_sync": sync_status,
    }


@app.get("/metrics")
async def metrics():
    return {
        "active_cameras": len(camera_manager.cameras),
        "buffered_detections": len(camera_manager.api_client.queue) if camera_manager.api_client else 0,
    }


def gen_frames(camera_id: str):
    detector = camera_manager.cameras.get(camera_id)
    if not detector:
        return
    while detector.running:
        frame = detector.get_latest_frame()
        if frame is not None:
            try:
                ret, buffer = cv2.imencode('.jpg', frame)
                if ret:
                    yield (b'--frame\r\n'
                           b'Content-Type: image/jpeg\r\n\r\n' + buffer.tobytes() + b'\r\n')
            except Exception as e:
                logger.error(f"Frame encode error: {e}")
        time.sleep(0.1)


@app.get("/video/{camera_id}")
async def video_feed(camera_id: str):
    if camera_id not in camera_manager.cameras:
        raise HTTPException(status_code=404, detail="Camera not found")
    return StreamingResponse(
        gen_frames(camera_id),
        media_type="multipart/x-mixed-replace; boundary=frame"
    )


@app.post("/cameras/{camera_id}/restart")
async def restart_camera(camera_id: str):
    if camera_id not in camera_manager.cameras:
        raise HTTPException(status_code=404, detail="Camera not found")
    camera_manager.restart_camera(camera_id)
    return {"status": "restarted", "camera_id": camera_id}


@app.post("/config/reload")
async def reload_config():
    return {"message": camera_manager.reload_config()}


@app.post("/sync")
async def sync_config():
    """Force an immediate config sync with the backend."""
    result = await camera_manager.force_sync()
    return {"status": "ok", **result}


@app.post("/command/{action}")
async def server_command(action: str):
    """Execute a server-wide command: start, pause, restart, stop."""
    valid_actions = ["start", "pause", "restart", "stop"]
    if action not in valid_actions:
        raise HTTPException(status_code=400, detail=f"Invalid action. Use: {valid_actions}")

    await camera_manager.handle_command(action)

    labels = {
        "start": "Serviços iniciados",
        "pause": "Serviços pausados",
        "restart": "Serviços reiniciados",
        "stop": "Serviços parados",
    }
    return {"status": "ok", "action": action, "message": labels[action]}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8765)

