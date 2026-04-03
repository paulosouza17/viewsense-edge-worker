"""
api_client.py — ViewSense API Client
Handles batched detection ingestion, heartbeat, ROI sync, and RTMP service control.
"""
import asyncio
import logging
import time
import socket
import os
import shutil
import subprocess
import psutil
import platform
from typing import Dict, List, Any, Optional
import httpx

logger = logging.getLogger(__name__)

class APIClient:
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        viewsense_conf = config['viewsense']

        self.server_id = viewsense_conf.get('server_id', 'mac-local')
        self.server_secret = viewsense_conf.get('server_secret', '')
        self.version = "1.0.0-mac"

        self.ingest_url = viewsense_conf.get('api_url', '')
        self.heartbeat_url = viewsense_conf.get('heartbeat_url', '')
        self.roi_sync_url = viewsense_conf.get('roi_sync_url', '')

        self.api_key = viewsense_conf.get('api_key', '')
        self.anon_key = viewsense_conf.get('anon_key', '')

        try:
            self.batch_size = int(viewsense_conf.get('batch_size', 20))
        except (ValueError, TypeError):
            self.batch_size = 20

        self.queue: List[Dict[str, Any]] = []

        self.headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "apikey": self.anon_key,
            "x-server-secret": self.server_secret,
        }

        if not self.anon_key:
            logger.warning("anon_key is missing! Requests will likely fail.")

        self.client = httpx.AsyncClient(headers=self.headers, timeout=10.0)
        self.loop = None  # Set by main
        self.running = True
        self.active_cameras = 0
        self.command_callback = None  # Set by camera_manager for dispatching commands

        # Derive Supabase base URL from heartbeat_url
        hb = self.heartbeat_url
        if '/functions/v1/' in hb:
            self.supabase_url = hb.split('/functions/v1/')[0]
        else:
            self.supabase_url = ''

    async def add_detection(self, detection: Dict[str, Any]):
        self.queue.append(detection)
        if len(self.queue) >= self.batch_size:
            logger.info(f"Batch size reached ({len(self.queue)}), flushing...")
            await self.flush()

    async def flush(self):
        if not self.queue:
            return

        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for det in self.queue:
            cam_id = det.get("camera_id")
            if not cam_id:
                continue
            if cam_id not in grouped:
                grouped[cam_id] = []
            det_clean = det.copy()
            det_clean.pop("camera_id", None)
            grouped[cam_id].append(det_clean)

        self.queue.clear()

        for cam_id, dets in grouped.items():
            payload = {"camera_id": cam_id, "detections": dets}
            try:
                await self._send_with_retry(self.ingest_url, payload)
                logger.info(f"✅ Sent {len(dets)} detections for camera {cam_id[:8]}…")
            except Exception as e:
                logger.error(f"Failed to send detections for {cam_id}: {e}")

    async def _send_with_retry(self, url: str, json_payload: Dict, max_retries: int = 3):
        if not url:
            raise ValueError("Missing API URL")

        for attempt in range(max_retries):
            try:
                response = await self.client.post(url, json=json_payload)
                response.raise_for_status()
                return
            except httpx.HTTPError as e:
                logger.warning(f"Attempt {attempt + 1}/{max_retries} failed: {e}")
                if attempt == max_retries - 1:
                    raise
                await asyncio.sleep(2 ** attempt)

    # ─── RTMP Service Management ──────────────────────────────────────────────

    @staticmethod
    def _get_rtmp_status() -> Dict[str, Any]:
        """Check RTMP service by probing the port directly.
        Uses a fast TCP connect (300ms timeout) — avoids slow pm2 jlist.
        """
        rtmp_port = 55935
        hls_port = 8001

        # Possible install locations for rtmp-ingest.cjs
        RTMP_SCRIPT_PATHS = [
            "/opt/viewsense/scripts/rtmp-ingest.cjs",
            "/opt/viewsense/rtmp-ingest.cjs",
            "/root/viewsense-rtmp/rtmp-ingest.cjs",
            "/root/rtmp-ingest.cjs",
        ]
        is_installed = any(os.path.exists(p) for p in RTMP_SCRIPT_PATHS)

        # Probe RTMP port (fast TCP connect — 300ms)
        try:
            with socket.create_connection(("127.0.0.1", rtmp_port), timeout=0.3):
                rtmp_running = True
        except (ConnectionRefusedError, OSError, socket.timeout):
            rtmp_running = False

        # If port is open, service is definitely installed and running
        if rtmp_running:
            is_installed = True

        if not is_installed:
            return {"rtmp_status": "not_installed"}

        return {
            "rtmp_status": "running" if rtmp_running else "stopped",
            "rtmp_port": rtmp_port,
            "rtmp_hls_port": hls_port,
        }


    @staticmethod
    async def _set_rtmp_enabled(enabled: bool):
        """Start or stop the viewsense-rtmp PM2 process."""
        pm2_path = shutil.which("pm2") or "/usr/lib/node_modules/pm2/bin/pm2"
        action = "start" if enabled else "stop"
        try:
            result = subprocess.run(
                [pm2_path, action, "viewsense-rtmp"],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0:
                logger.info(f"📡 RTMP service {action}ed successfully")
            else:
                logger.warning(f"📡 RTMP {action} returned code {result.returncode}: {result.stderr[:100]}")
        except Exception as e:
            logger.error(f"Failed to {action} RTMP service: {e}")

    # ─── Heartbeat ────────────────────────────────────────────────────────────

    async def send_heartbeat(self):
        if not self.heartbeat_url:
            return

        # Gather RTMP status (fast port probe, non-blocking feel)
        rtmp_info = self._get_rtmp_status()

        payload = {
            "server_id": self.server_id,
            "server_secret": self.server_secret,
            "cpu_usage": psutil.cpu_percent(interval=0.1),
            "ram_usage": psutil.virtual_memory().used // (1024 * 1024),
            "ram_total": psutil.virtual_memory().total // (1024 * 1024),
            "uptime_seconds": int(time.time() - psutil.boot_time()),
            "cameras_active": self.active_cameras,
            "hostname": platform.node(),
            "version": self.version,
            **rtmp_info,  # rtmp_status, rtmp_port, rtmp_hls_port
        }

        try:
            response = await self.client.post(self.heartbeat_url, json=payload)
            response.raise_for_status()
            logger.info("💓 Heartbeat sent")

            # Parse response for commands and rtmp_enabled directive
            try:
                data = response.json()
                if not isinstance(data, dict):
                    return

                # Handle pending worker command (start/pause/restart/stop)
                pending_cmd = data.get("pending_command")
                if pending_cmd and self.command_callback:
                    logger.info(f"📡 Received command from dashboard: {pending_cmd}")
                    await self.command_callback(pending_cmd)
                    logger.info(f"✅ Command '{pending_cmd}' executed")

                # Handle RTMP enable/disable directive from dashboard toggle
                rtmp_enabled = data.get("rtmp_enabled")
                if rtmp_enabled is not None:
                    current_status = rtmp_info.get("rtmp_status", "not_installed")
                    is_running = current_status == "running"
                    is_installed = current_status != "not_installed"

                    if is_installed:
                        if rtmp_enabled and not is_running:
                            logger.info("📡 Dashboard requested RTMP start")
                            await self._set_rtmp_enabled(True)
                        elif not rtmp_enabled and is_running:
                            logger.info("📡 Dashboard requested RTMP stop")
                            await self._set_rtmp_enabled(False)

            except Exception:
                pass  # Response parsing is best-effort

        except Exception as e:
            logger.error(f"Heartbeat failed: {e}")

    async def heartbeat_loop(self):
        interval = self.config["viewsense"].get("heartbeat_interval_seconds", 60)
        logger.info(f"Heartbeat loop started (interval={interval}s)")
        while self.running:
            await self.send_heartbeat()
            await asyncio.sleep(interval)

    async def flush_loop(self):
        interval = self.config["viewsense"].get("send_interval_seconds", 10)
        logger.info(f"Flush loop started (interval={interval}s)")
        while self.running:
            await asyncio.sleep(interval)
            await self.flush()

    async def roi_sync_loop(self):
        pass  # Handled by ROISyncManager

    async def sync_rois(self) -> Optional[Dict[str, Any]]:
        if not self.roi_sync_url or not self.server_id:
            return None

        try:
            response = await self.client.get(
                self.roi_sync_url,
                params={"server_id": self.server_id}
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.warning(f"ROI sync failed: {e}")
            return {"error": str(e)}

    async def close(self):
        self.running = False
        await self.flush()
        await self.client.aclose()
