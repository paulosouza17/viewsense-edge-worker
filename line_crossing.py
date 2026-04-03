
"""
Módulo de detecção de cruzamento de linha.
Usa o centróide inferior (bottom-center) do bounding box do tracker
para determinar se um objeto cruzou uma linha de contagem.
"""
import logging
import numpy as np
from dataclasses import dataclass, field
from typing import Optional, List, Tuple, Dict

# Configure Logger
logger = logging.getLogger(__name__)

@dataclass
class CountingLine:
    """Representa uma linha de contagem vinda do ROI sync."""
    roi_id: str
    camera_id: str
    name: str
    # Pontos da linha em pixels (convertidos das coordenadas normalizadas)
    p1: Tuple[float, float]  # (x1, y1)
    p2: Tuple[float, float]  # (x2, y2)
    direction: str  # "both", "in", "out"
    
    # Vetor normal da linha (para determinar lado)
    _normal: Tuple[float, float] = field(init=False, repr=False)
    
    def __post_init__(self):
        dx = self.p2[0] - self.p1[0]
        dy = self.p2[1] - self.p1[1]
        # Normal perpendicular (rotação 90° anti-horário)
        length = np.sqrt(dx**2 + dy**2)
        if length > 0:
            self._normal = (-dy / length, dx / length)
        else:
            self._normal = (0, 1)
    
    def side_of_point(self, point: Tuple[float, float]) -> float:
        """
        Retorna valor positivo ou negativo indicando de qual lado
        da linha o ponto está. Sinal muda = cruzou a linha.
        """
        dx = point[0] - self.p1[0]
        dy = point[1] - self.p1[1]
        return dx * self._normal[0] + dy * self._normal[1]
    
    @classmethod
    def from_roi(cls, roi: dict, frame_width: int, frame_height: int) -> Optional["CountingLine"]:
        """Cria CountingLine a partir de uma ROI do roi-sync."""
        if roi.get("roi_type") != "line" or not roi.get("is_counting_line"):
            return None
        
        coords = roi.get("coordinates", [])
        if len(coords) < 2:
            return None
        
        # Ensure coordinates are float and normalized 0-1 before multiplying
        # Ideally ROI sync provides normalized coords.
        
        x1 = float(coords[0].get("x", 0))
        y1 = float(coords[0].get("y", 0))
        x2 = float(coords[1].get("x", 0))
        y2 = float(coords[1].get("y", 0))

        p1 = (x1 * frame_width, y1 * frame_height)
        p2 = (x2 * frame_width, y2 * frame_height)
        
        return cls(
            roi_id=roi["id"],
            camera_id=roi["camera_id"],
            name=roi.get("name", "Unnamed Line"),
            p1=p1,
            p2=p2,
            direction=roi.get("direction", "both"),
        )


class LineCrossingDetector:
    """
    Detecta quando tracks cruzam linhas de contagem.
    Mantém histórico do 'lado' de cada track para cada linha.
    """
    
    def __init__(self):
        # {(track_id, roi_id): último_side}
        self._track_sides: Dict[Tuple[str, str], float] = {}
        # Tracks já contados para evitar dupla contagem por linha
        # {(track_id, roi_id): direction}
        self._counted: Dict[Tuple[str, str], str] = {}
        # Último cy de cada track para detectar direção vertical de movimento
        # {track_id: cy_anterior}
        self._prev_cy: Dict[str, float] = {}
    
    def update(
        self,
        track_id: str,
        bbox: dict,  # {"x": int, "y": int, "width": int, "height": int}
        lines: List[CountingLine],
    ) -> List[dict]:
        """
        Verifica se o track cruzou alguma linha.
        
        Args:
            track_id: ID do tracker (ex: "track_42")
            bbox: Bounding box com x, y, width, height em pixels
            lines: Lista de CountingLine ativas
        
        Returns:
            Lista de eventos de cruzamento:
            [{"roi_id": str, "direction": "in"|"out", "crossed_line": True}]
        """
        if not lines:
            return []

        cx = bbox["x"] + bbox["width"] / 2
        cy_center = bbox["y"] + bbox["height"] / 2
        cy_head   = bbox["y"]  # topo da caixa = cabeça

        # --- Ponto de Borda Líder Adaptativo ---
        # Se a pessoa está subindo no frame (cy diminuindo), a cabeça chega
        # primeiro na linha → usamos o topo da caixa para máxima janela.
        # Se está descendo (cy aumentando), a barriga/centro chega primeiro
        # → usamos o centro. Sem histórico ainda → centro como fallback.
        prev_cy = self._prev_cy.get(track_id)
        if prev_cy is not None and prev_cy - cy_center > 2:  # movendo para cima
            cy = cy_head
        else:  # movendo para baixo ou sem histórico
            cy = cy_center

        self._prev_cy[track_id] = cy_center
        point = (cx, cy)
        
        crossings = []
        
        for line in lines:
            key = (track_id, line.roi_id)
            
            # Já foi contado nesta linha? Pula
            if key in self._counted:
                continue
            
            current_side = line.side_of_point(point)
            
            # DEBUG: Log Proximity (< 50 pixels from line)
            # This helps user visualize where the "invisible line" is relative to people
            if abs(current_side) < 50:
                 # Rate limit logs? No, we need frame-by-frame here for diagnosis.
                 # Using print/warning to ensure visibility if log level is high.
                 pass 
                 # logger.info(f"PROXIMITY: Track {track_id} dist={current_side:.1f} to line '{line.name}' at {point}")

            if key in self._track_sides:
                prev_side = self._track_sides[key]
                
                # Houve cruzamento? (sinais opostos)
                if prev_side * current_side < 0:
                    # Determinar direção: 
                    # positivo→negativo = "in", negativo→positivo = "out"
                    
                    direction = "in" if prev_side > 0 else "out"
                    
                    # Filtrar por direção configurada na linha
                    if line.direction == "both" or line.direction == direction:
                        logger.info(f"CROSSING DETECTED: {track_id} -> {direction} on line '{line.name}' ({line.roi_id})")
                        crossings.append({
                            "roi_id": line.roi_id,
                            "direction": direction,
                            "crossed_line": True,
                        })
                        self._counted[key] = direction
                    else:
                        logger.warning(f"IGNORED CROSSING: {track_id} -> {direction} on line '{line.name}' (Config expects: {line.direction})")
            
            self._track_sides[key] = current_side
        
        return crossings
    
    def cleanup_stale_tracks(self, active_track_ids: set):
        """Remove tracks que não estão mais ativos (saíram do frame)."""
        stale_keys = [
            k for k in self._track_sides.keys()
            if k[0] not in active_track_ids
        ]
        for k in stale_keys:
            del self._track_sides[k]
            self._counted.pop(k, None)

        # Limpar histórico de cy de tracks inativos
        stale_tracks = [tid for tid in self._prev_cy if tid not in active_track_ids]
        for tid in stale_tracks:
            del self._prev_cy[tid]
