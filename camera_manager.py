"""
camera_manager.py — Mac-adapted Camera Manager
"""
import yaml
import logging
import asyncio
from typing import Dict, List, Tuple, Set, Any
from api_client import APIClient
from detector import CameraDetector
from roi_sync import ROISyncManager
from config_sync import ConfigSync

logger = logging.getLogger(__name__)


class CameraManager:
    def __init__(self, config_path: str = "config.yaml"):
        self.config_path = config_path
        self.cameras: Dict[str, CameraDetector] = {}
        self.config = self._load_config()
        self.api_client = None
        self.roi_manager = None
        self.config_sync = None
        self.api_client_loop = None

    def _load_config(self):
        with open(self.config_path, 'r') as f:
            return yaml.safe_load(f)

    def start(self):
        self.api_client = APIClient(self.config)
        self.roi_manager = ROISyncManager(self.api_client, self.config)
        self.roi_manager.on_roi_change(self.on_roi_updated)

        if self.api_client_loop:
            self.api_client.loop = self.api_client_loop

        # Wire up command callback for dashboard controls
        self.api_client.command_callback = self.handle_command

        for cam_conf in self.config['cameras']:
            self._start_camera(cam_conf)

        if self.api_client:
            self.api_client.active_cameras = len(self.cameras)
            asyncio.create_task(self.roi_manager.start())

        # Start config auto-sync
        self.config_sync = ConfigSync(self.config, self.api_client.client)
        self.config_sync.on_diff(self._apply_camera_diff)
        asyncio.create_task(self.config_sync.loop(self._get_current_state))

    def on_roi_updated(self, camera_id: str, rois: list, camera_config: dict):
        if camera_id in self.cameras:
            self.cameras[camera_id].update_settings(rois, camera_config)
            logger.info(f"Updated settings for camera {camera_id}")

    def _start_camera(self, cam_conf):
        cam_id = cam_conf['id']
        if cam_id in self.cameras:
            return
        detector = CameraDetector(cam_conf, self.api_client)

        # Se o ROI Manager já possuir os recortes desta câmera em memória (ex: num restart do ConfigSync), injeta na hora!
        if self.roi_manager and cam_id in self.roi_manager.current_rois:
            detector.update_settings(
                self.roi_manager.current_rois[cam_id],
                self.roi_manager.current_cameras.get(cam_id, cam_conf)
            )

        detector.start()
        self.cameras[cam_id] = detector
        logger.info(f"Started camera {cam_id}")

    def stop_camera(self, cam_id: str):
        if cam_id in self.cameras:
            self.cameras[cam_id].stop()
            del self.cameras[cam_id]

    def restart_camera(self, cam_id: str):
        cam_conf = next((c for c in self.config['cameras'] if c['id'] == cam_id), None)
        if not cam_conf:
            return False
        self.stop_camera(cam_id)
        self._start_camera(cam_conf)
        return True

    def reload_config(self):
        self.config = self._load_config()
        return "Config reloaded"

    # ---------- Config Sync integration ----------

    def _get_current_state(self) -> Tuple[Set[str], Dict[str, Dict]]:
        """Return current camera IDs and their configs for the sync loop."""
        ids = set(self.cameras.keys())
        configs = {}
        for cam_conf in self.config.get('cameras', []):
            configs[cam_conf['id']] = cam_conf
        return ids, configs

    async def _apply_camera_diff(
        self,
        added: List[Dict[str, Any]],
        removed: List[str],
        updated: List[Dict[str, Any]],
    ):
        """Hot-reload cameras based on diff from ConfigSync."""
        # Remove cameras
        for cam_id in removed:
            logger.info(f"🗑️  Removing camera {cam_id}")
            self.stop_camera(cam_id)
            self.config['cameras'] = [
                c for c in self.config['cameras'] if c['id'] != cam_id
            ]

        # Add new cameras
        for cam_conf in added:
            logger.info(f"➕ Adding camera {cam_conf.get('name', cam_conf['id'])}")
            self.config['cameras'].append(cam_conf)
            self._start_camera(cam_conf)

        # Update changed cameras (stop + start with new config)
        for cam_conf in updated:
            cam_id = cam_conf['id']
            logger.info(f"🔄 Updating camera {cam_conf.get('name', cam_id)}")
            self.stop_camera(cam_id)
            # Replace config entry
            self.config['cameras'] = [
                c for c in self.config['cameras'] if c['id'] != cam_id
            ]
            self.config['cameras'].append(cam_conf)
            await asyncio.sleep(1)  # Brief pause before restart
            self._start_camera(cam_conf)

        # Update active camera count
        if self.api_client:
            self.api_client.active_cameras = len(self.cameras)

    async def force_sync(self) -> Dict[str, Any]:
        """Trigger an immediate config sync (called from API endpoint)."""
        if not self.config_sync:
            return {"error": "ConfigSync not initialized"}
        camera_ids, camera_configs = self._get_current_state()
        return await self.config_sync.sync_once(camera_ids, camera_configs)

    # ---------- Commands ----------

    async def handle_command(self, command: str):
        """Handle a remote command from the dashboard."""
        logger.info(f"🎮 Executing command: {command}")

        if command == 'pause' or command == 'stop':
            count = len(self.cameras)
            for cam_id in list(self.cameras.keys()):
                self.stop_camera(cam_id)
            if self.api_client:
                self.api_client.active_cameras = 0
            logger.info(f"⏸ Paused {count} cameras")

        elif command == 'start':
            if len(self.cameras) > 0:
                logger.info("▶️ Cameras already running, skipping start")
                return
            for cam_conf in self.config['cameras']:
                self._start_camera(cam_conf)
            if self.api_client:
                self.api_client.active_cameras = len(self.cameras)
            logger.info(f"▶️ Started {len(self.cameras)} cameras")

        elif command == 'restart':
            # Stop all
            for cam_id in list(self.cameras.keys()):
                self.stop_camera(cam_id)
            await asyncio.sleep(2)
            # Start all
            for cam_conf in self.config['cameras']:
                self._start_camera(cam_conf)
            if self.api_client:
                self.api_client.active_cameras = len(self.cameras)
            logger.info(f"🔄 Restarted {len(self.cameras)} cameras")

        else:
            logger.warning(f"Unknown command: {command}")

    async def shutdown(self):
        if self.config_sync:
            self.config_sync.stop()
        for cam_id in list(self.cameras.keys()):
            self.stop_camera(cam_id)
        if self.api_client:
            await self.api_client.close()


