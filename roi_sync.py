import asyncio
import json
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List, Any

logger = logging.getLogger(__name__)

# Brasília = UTC-3
BRASILIA_TZ = timezone(timedelta(hours=-3))

# Canonical path where the RTMP server reads the whitelist
ACTIVE_STREAMS_PATH = "/opt/viewsense/active_streams.json"

class ROISyncManager:
    """Gerencia sincronização periódica de ROIs com o painel ViewSense."""

    def __init__(self, api_client, config: dict):
        """
        Args:
            api_client: Instância do ViewSenseClient (com método sync_rois())
            config: Dict do config.yaml completo
        """
        self.api_client = api_client
        self.config = config
        self.current_rois: dict = {}          # {camera_id: [roi_configs]}
        self.current_cameras: dict = {}       # {camera_id: camera_config}
        
        # Default to 60s if not in config
        viewsense_conf = config.get("viewsense", {})
        self.sync_interval: int = viewsense_conf.get("roi_sync_interval_seconds", 60)
        
        self._running = False
        self._callbacks = []  # Funções chamadas quando ROIs mudam

    def on_roi_change(self, callback):
        """Registra callback chamado quando ROIs ou Configs de Câmera são atualizadas.
        
        O callback recebe: callback(camera_id: str, rois: list[dict], camera_config: dict)
        """
        self._callbacks.append(callback)

    async def start(self):
        """Inicia o loop de sincronização."""
        if self._running:
            return
            
        self._running = True
        logger.info(f"ROI Sync iniciado (intervalo: {self.sync_interval}s)")
        
        # Primeira sync imediata
        await self._sync()
        
        while self._running:
            await asyncio.sleep(self.sync_interval)
            await self._sync()

    def stop(self):
        """Para o loop de sincronização."""
        self._running = False
        logger.info("ROI Sync parado")

    async def _sync(self):
        """Executa uma sincronização."""
        try:
            result = await self.api_client.sync_rois()
            if result is None:
                # Pode acontecer se URLs estiverem vazias
                return

            if "error" in result:
                logger.error(f"Erro no ROI sync: {result['error']}")
                return

            new_rois = result.get("rois", [])
            new_cameras = result.get("cameras", [])
            pending_update = result.get("pending_update")
            
            # Agrupar ROIs por camera_id
            rois_by_camera: dict = {}
            for roi in new_rois:
                cam_id = roi["camera_id"]
                if cam_id not in rois_by_camera:
                    rois_by_camera[cam_id] = []
                rois_by_camera[cam_id].append(roi)

            # Map camera configs
            new_cameras_map = {c["id"]: c for c in new_cameras}
            
            # Detectar mudanças
            changed_cameras = self._detect_changes(rois_by_camera, new_cameras_map)
            
            # Atualizar estado interno
            self.current_rois = rois_by_camera
            self.current_cameras = new_cameras_map

            if changed_cameras:
                logger.info(f"ROIs/Configs atualizadas para câmeras: {changed_cameras}")
                for cam_id in changed_cameras:
                    rois = rois_by_camera.get(cam_id, [])
                    cam_config = new_cameras_map.get(cam_id, {})
                    
                    for cb in self._callbacks:
                        try:
                            cb(cam_id, rois, cam_config)
                        except Exception as e:
                            logger.error(f"Erro em callback ROI: {e}")

            # Always refresh active_streams.json based on current schedules
            self._update_active_streams(new_cameras_map)

            # Tratar atualização de versão pendente (log apenas por enquanto)
            if pending_update:
                logger.warning(
                    f"⚠️ Atualização pendente: v{pending_update.get('target_version')} "
                    f"— {pending_update.get('update_notes', 'sem notas')}"
                )

        except Exception as e:
            logger.error(f"Falha no ROI sync: {e}")

    def _detect_changes(self, new_rois_by_camera: dict, new_cameras_map: dict) -> list:
        """Compara ROIs e Configs atuais com novas."""
        changed = []
        
        all_cameras = set(list(self.current_rois.keys()) + list(new_rois_by_camera.keys()) + 
                          list(self.current_cameras.keys()) + list(new_cameras_map.keys()))
        
        for cam_id in all_cameras:
            # Check ROI changes
            old_rois = self.current_rois.get(cam_id, [])
            new_rois = new_rois_by_camera.get(cam_id, [])
            
            rois_changed = False
            
            # Compare IDs and timestamps/versions if available, else content
            # Simplest for now: string representation or exact content compare
            if len(old_rois) != len(new_rois):
                rois_changed = True
            else:
                # Deep compare coordinates & metadata
                # Sort by ID to align
                old_rois.sort(key=lambda x: x.get('id', ''))
                new_rois.sort(key=lambda x: x.get('id', ''))
                
                if old_rois != new_rois:
                    rois_changed = True
            
            # Check Camera Config changes
            old_conf = self.current_cameras.get(cam_id, {})
            new_conf = new_cameras_map.get(cam_id, {})
            
            # Check specific fields that affect detection
            relevant_fields = ['confidence_threshold', 'enabled_classes', 'fps', 'demographics_enabled']
            
            # If camera config is completely new or missing
            if not old_conf and new_conf:
                rois_changed = True
            elif old_conf and not new_conf:
                pass # Camera removed - handled?
            else:
                 for field in relevant_fields:
                    if old_conf.get(field) != new_conf.get(field):
                        rois_changed = True
                        break

            if rois_changed:
                changed.append(cam_id)

        return changed

    def get_counting_lines(self, camera_id: str) -> list:
        """Retorna apenas linhas de contagem ativas para uma câmera."""
        rois = self.current_rois.get(camera_id, [])
        return [
            roi for roi in rois
            if roi.get("is_counting_line") and roi.get("is_active")
        ]

    def get_camera_config(self, camera_id: str) -> Optional[dict]:
        return self.current_cameras.get(camera_id)

    # ─── Schedule / Active Streams ───────────────────────────────────────────

    @staticmethod
    def _is_camera_active(cam: dict) -> bool:
        """Returns True if camera should be streaming now (Brasília time).

        Rules:
        - ai_enabled=False  → never active (AI turned off in frontend)
        - schedule_enabled=False or missing → always active (no restriction)
        - schedule_enabled=True → check day-of-week and time window
        """
        if not cam.get('ai_enabled', True):
            return False

        if not cam.get('schedule_enabled', False):
            return True  # No schedule restriction — always allow

        now_brt = datetime.now(BRASILIA_TZ)
        current_weekday = now_brt.weekday()  # Mon=0 … Sun=6
        # DB stores 0=Sunday … 6=Saturday (JS convention)
        # Convert Python weekday to JS: Mon=1, Tue=2, …, Sun=0
        js_weekday = (current_weekday + 1) % 7

        schedule_days = cam.get('schedule_days') or list(range(7))
        if js_weekday not in schedule_days:
            return False

        # Parse HH:MM:SS strings
        def parse_time(t: str):
            try:
                parts = t.split(':')
                return now_brt.replace(
                    hour=int(parts[0]), minute=int(parts[1]),
                    second=int(parts[2]) if len(parts) > 2 else 0,
                    microsecond=0
                )
            except Exception:
                return None

        start_str = cam.get('schedule_start') or '00:00:00'
        end_str   = cam.get('schedule_end')   or '23:59:59'
        start_dt  = parse_time(start_str)
        end_dt    = parse_time(end_str)

        if start_dt and end_dt:
            return start_dt <= now_brt <= end_dt
        return True  # Fail-open if parse fails

    def _update_active_streams(self, cameras_map: dict):
        """Rewrite active_streams.json with hashes of cameras currently in-schedule.

        - Cameras within their operating window: hash included → RTMP ACCEPTED
        - Cameras outside window / ai_enabled=False: hash excluded → RTMP REJECTED
        - Empty list [] would mean allow-all; we never write that — we write the
          explicit list of active hashes so the RTMP server always enforces it.
        """
        active_hashes: List[str] = []

        for cam in cameras_map.values():
            stream_url: str = cam.get('stream_url', '') or ''
            if not stream_url:
                continue

            # Extract hash from rtmp://…/live/<hash>
            parts = stream_url.rstrip('/').split('/')
            stream_hash = parts[-1] if parts else ''
            if len(stream_hash) != 48:
                continue  # Not a valid hash — skip

            if self._is_camera_active(cam):
                active_hashes.append(stream_hash)

        try:
            os.makedirs(os.path.dirname(ACTIVE_STREAMS_PATH), exist_ok=True)
            with open(ACTIVE_STREAMS_PATH, 'w') as f:
                json.dump(active_hashes, f, indent=2)
            logger.info(
                f"📋 active_streams.json atualizado: {len(active_hashes)} câmera(s) ativas "
                f"[BRT {datetime.now(BRASILIA_TZ).strftime('%H:%M')}]"
            )
        except Exception as e:
            logger.error(f"Falha ao gravar active_streams.json: {e}")
