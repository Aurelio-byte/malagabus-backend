"""
Servicio de estimación de tiempo de llegada (ETA) usando GTFS y posiciones en tiempo real.
Lógica: vehículos de la línea + distancia aproximada a parada; fallback a horario/estimación.
"""
import math
from typing import Any, Dict, List, Optional, Tuple


# Metros por minuto de avance aproximado en ciudad (~9 km/h)
METERS_PER_MINUTE = 150
# ETA mínimo cuando hay vehículo cercano (minutos)
MIN_ETA_MINUTES = 1
# ETA fallback cuando no hay realtime (minutos)
FALLBACK_ETA_MINUTES = 8


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return r * c


def _normalize_route_id(route_id: Optional[str]) -> str:
    if route_id is None:
        return ""
    s = str(route_id).strip()
    if not s:
        return ""
    try:
        return str(int(float(s)))
    except (TypeError, ValueError):
        return s


def _get_vehicles_for_route(positions: List[Dict], route_id: str) -> List[Dict]:
    """Filtra posiciones de vehículos de la línea dada."""
    norm = _normalize_route_id(route_id)
    if not norm:
        return []
    out = []
    for p in positions:
        line = p.get("line")
        if line is None and p.get("raw"):
            raw_line = p["raw"].get("codLinea")
            if raw_line is not None:
                try:
                    line = str(int(float(raw_line)))
                except (TypeError, ValueError):
                    line = str(raw_line)
        if line is None:
            continue
        if _normalize_route_id(line) == norm:
            out.append(p)
    return out


def _lat_lon_from_position(p: Dict) -> Tuple[float, float]:
    lat = p.get("lat")
    lon = p.get("lon")
    if lat is None or lon is None:
        return None, None
    try:
        return float(lat), float(lon)
    except (TypeError, ValueError):
        return None, None


def estimate_eta_from_distance_m(distance_m: float) -> int:
    """Convierte distancia en metros a ETA en minutos (aproximación urbana)."""
    if distance_m <= 0:
        return MIN_ETA_MINUTES
    minutes = distance_m / METERS_PER_MINUTE
    return max(MIN_ETA_MINUTES, round(minutes))


def compute_eta(
    stop_id: str,
    route_id: str,
    gtfs_service: Any,
    realtime_service: Any,
    realtime_last_updated: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Estima ETA en minutos para una parada y línea.
    - Si hay vehículos de esa línea en realtime: ETA por distancia al más cercano; confidence media/alta.
    - Si no hay vehículos o no hay parada: fallback a estimación con confidence baja.
    Mantiene formato compatible con el endpoint existente.
    """
    result = {
        "stop_id": stop_id,
        "route_id": route_id,
        "eta_minutes": FALLBACK_ETA_MINUTES,
        "confidence": "baja",
        "source": "scheduled_fallback",
        "realtime_last_updated": realtime_last_updated or getattr(realtime_service, "last_updated", None),
    }

    stop_coords = gtfs_service.get_stop_coords(stop_id) if gtfs_service else None
    positions = getattr(realtime_service, "positions", []) or []

    # Vehículos de la línea
    vehicles = _get_vehicles_for_route(positions, route_id)

    if vehicles and stop_coords:
        stop_lat, stop_lon = stop_coords
        best_eta = None
        best_distance_m = None
        for v in vehicles:
            v_lat, v_lon = _lat_lon_from_position(v)
            if v_lat is None:
                continue
            d = _haversine_m(stop_lat, stop_lon, v_lat, v_lon)
            eta = estimate_eta_from_distance_m(d)
            if best_eta is None or eta < best_eta:
                best_eta = eta
                best_distance_m = d
        if best_eta is not None:
            result["eta_minutes"] = best_eta
            result["source"] = "realtime_distance"
            result["confidence"] = "alta" if (best_distance_m is not None and best_distance_m < 500) else "media"
            result["distance_m"] = round(best_distance_m) if best_distance_m is not None else None
            return result

    # Sin vehículos de la línea pero con realtime cargado: indicar que la línea existe pero no hay bus cercano
    if positions and not vehicles:
        result["source"] = "scheduled_fallback"
        result["confidence"] = "baja"
        # Opcional: ETA más conservador si hay otras líneas con datos
        result["eta_minutes"] = FALLBACK_ETA_MINUTES
        return result

    # Sin datos realtime: fallback horario/estimación
    result["eta_minutes"] = FALLBACK_ETA_MINUTES
    result["confidence"] = "baja"
    result["source"] = "scheduled_fallback"
    return result


def eta_nearby_for_route(
    route_id: str,
    lat: float,
    lon: float,
    gtfs_service: Any,
    realtime_service: Any,
    limit_stops: int = 12,
    limit_results: int = 5,
) -> Dict[str, Any]:
    """
    Para demo: siguiente bus cercano por línea. Paradas cercanas a (lat, lon) servidas por la ruta,
    con ETA estimado; ordenado por ETA ascendente.
    """
    stops_served = gtfs_service.stops_served_by_route(route_id) if gtfs_service else set()
    nearby = gtfs_service.nearest_stops(lat, lon, limit=limit_stops) if gtfs_service else []
    realtime_last_updated = getattr(realtime_service, "last_updated", None)

    results = []
    for s in nearby:
        sid = s.get("stop_id")
        if not sid or sid not in stops_served:
            continue
        eta_result = compute_eta(sid, route_id, gtfs_service, realtime_service, realtime_last_updated)
        results.append({
            "stop_id": sid,
            "stop_name": s.get("name"),
            "distance_m": s.get("distance_m"),
            "eta_minutes": eta_result["eta_minutes"],
            "confidence": eta_result["confidence"],
            "source": eta_result["source"],
        })
    results.sort(key=lambda x: (x["eta_minutes"], x.get("distance_m") or 99999))
    return {
        "route_id": route_id,
        "origin": {"lat": lat, "lon": lon},
        "realtime_last_updated": realtime_last_updated,
        "stops": results[:limit_results],
    }
