import os
import json
import threading
import urllib.parse
import urllib.request
from pathlib import Path
from typing import List, Optional
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.services.eta_service import compute_eta, eta_nearby_for_route
from app.services.gtfs_service import GTFSService
from app.services.realtime_service import RealtimeService


app = FastAPI(title="MalagaBus API", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

gtfs_service = GTFSService()
realtime_service = RealtimeService()
BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"
app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")
IS_RENDER = os.environ.get("RENDER", "").lower() == "true"
_data_load_lock = threading.Lock()
MAX_SERVICE_AREA_DISTANCE_M = int(os.environ.get("MALAGABUS_MAX_SERVICE_DISTANCE_M", "2500"))


class Preferences(BaseModel):
    avoid_transfers: bool = False
    minimize_walking: bool = True
    simple_mode: bool = True


class RoutePlanRequest(BaseModel):
    origin_lat: float
    origin_lon: float
    destination_text: str = Field(min_length=2)
    lang: str = "es"
    preferences: Preferences = Preferences()


class RouteStep(BaseModel):
    order: int
    step_type: str
    instruction: str
    eta_minutes: Optional[int] = None


class RouteOption(BaseModel):
    route_id: str
    line: str
    origin_stop: str
    destination_stop: str
    total_minutes: int
    confidence: str
    steps: List[RouteStep]
    wait_eta_minutes: Optional[int] = None
    walk_to_origin_distance_m: Optional[int] = None
    walk_to_origin_duration_min: Optional[int] = None
    walk_to_origin_path: Optional[List[List[float]]] = None


class RoutePlanResponse(BaseModel):
    recommended_route_id: str
    route_options: List[RouteOption]


_journey_state = {}


@app.get("/")
def root():
    # El launcher abre la raíz del puerto: enviamos UI para usuario final.
    return RedirectResponse(url="/app")


@app.get("/app")
def app_home():
    return FileResponse(str(WEB_DIR / "index.html"))


@app.on_event("startup")
def startup_bootstrap():
    # En Render Free (512MB), cargar GTFS completo al arranque puede tumbar el proceso por memoria.
    # Lo evitamos en cloud y dejamos la carga bajo demanda mediante /v1/data/refresh.
    if IS_RENDER:
        return
    # Carga datos en arranque si no existe cache local.
    if not gtfs_service.stops:
        try:
            gtfs_service.refresh()
        except Exception:
            pass
    if not realtime_service.positions:
        try:
            realtime_service.refresh()
        except Exception:
            pass


def _ensure_data_ready() -> None:
    """
    Garantiza que GTFS esté cargado antes de planificar.
    En Render se carga bajo demanda para evitar picos de memoria en startup.
    """
    if gtfs_service.stops:
        return
    with _data_load_lock:
        if gtfs_service.stops:
            return
        try:
            gtfs_service.refresh()
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail=f"No se han podido cargar datos GTFS aún: {exc}",
            )
        if not gtfs_service.stops:
            raise HTTPException(
                status_code=503,
                detail="GTFS vacío tras refresh. Intenta de nuevo en unos segundos.",
            )
        if not realtime_service.positions:
            try:
                realtime_service.refresh()
            except Exception:
                pass


def _wait_eta_minutes(origin_stop_id: str, line: str) -> int:
    """
    ETA real/fallback para la parada de subida de una línea.
    """
    try:
        eta = compute_eta(
            stop_id=origin_stop_id,
            route_id=str(line),
            gtfs_service=gtfs_service,
            realtime_service=realtime_service,
            realtime_last_updated=realtime_service.last_updated,
        )
        v = int(eta.get("eta_minutes", 8))
        return max(1, min(v, 60))
    except Exception:
        return 8


def _fetch_walk_path(lat_from: float, lon_from: float, lat_to: float, lon_to: float) -> dict:
    """
    Ruta peatonal real (OSRM). Devuelve dict con path [ [lat,lon], ... ].
    Si falla red/API, devuelve {} y mantenemos fallback local.
    """
    try:
        coords = f"{lon_from},{lat_from};{lon_to},{lat_to}"
        params = urllib.parse.urlencode({"overview": "full", "geometries": "geojson", "steps": "false"})
        url = f"https://router.project-osrm.org/route/v1/foot/{coords}?{params}"
        req = urllib.request.Request(url, headers={"User-Agent": "MalagaBus/1.0"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
        routes = payload.get("routes") or []
        if not routes:
            return {}
        r0 = routes[0]
        geom = (r0.get("geometry") or {}).get("coordinates") or []
        # OSRM devuelve [lon,lat] -> convertir a [lat,lon]
        path = [[float(c[1]), float(c[0])] for c in geom if isinstance(c, list) and len(c) >= 2]
        if len(path) < 2:
            return {}
        return {
            "path": path,
            "distance_m": int(round(float(r0.get("distance") or 0))),
            "duration_min": max(1, int(round(float(r0.get("duration") or 0) / 60.0))),
        }
    except Exception:
        return {}


def _enrich_walk_to_origin(option: RouteOption, origin_lat: float, origin_lon: float) -> RouteOption:
    """
    Añade path peatonal real y reescribe paso 1/2 con datos de ETA/recorrido.
    """
    stop = next((s for s in gtfs_service.stops if s.get("stop_name") == option.origin_stop), None)
    if not stop:
        return option
    try:
        stop_lat = float(stop.get("stop_lat"))
        stop_lon = float(stop.get("stop_lon"))
    except Exception:
        return option

    walk = _fetch_walk_path(origin_lat, origin_lon, stop_lat, stop_lon)
    if walk:
        option.walk_to_origin_path = walk.get("path")
        option.walk_to_origin_distance_m = int(walk.get("distance_m", option.walk_to_origin_distance_m or 0))
        option.walk_to_origin_duration_min = int(walk.get("duration_min", option.walk_to_origin_duration_min or 0))
        for st in option.steps:
            if st.order == 1 and st.step_type == "walk":
                st.instruction = f"Camina por el recorrido marcado hasta {option.origin_stop}."
                st.eta_minutes = option.walk_to_origin_duration_min
                break
    return option


@app.get("/v1/health")
def health():
    return {
        "status": "ok",
        "service": "malagabus-backend",
        "gtfs_loaded": len(gtfs_service.stops) > 0,
        "gtfs_stops": len(gtfs_service.stops),
        "realtime_positions": len(realtime_service.positions),
    }


@app.get("/v1/data/status")
def data_status():
    return {
        "gtfs": {
            "stops": len(gtfs_service.stops),
            "routes": len(gtfs_service.routes),
            "trips": len(gtfs_service.trips),
            "stop_times": len(gtfs_service.stop_times),
            "last_updated": gtfs_service.last_updated,
        },
        "realtime": {
            "positions": len(realtime_service.positions),
            "last_updated": realtime_service.last_updated,
        },
    }


@app.post("/v1/data/refresh")
def refresh_data():
    result = {"gtfs": None, "realtime": None, "errors": []}
    try:
        result["gtfs"] = gtfs_service.refresh()
    except Exception as exc:
        result["errors"].append(f"gtfs: {exc}")
    try:
        result["realtime"] = realtime_service.refresh()
    except Exception as exc:
        result["errors"].append(f"realtime: {exc}")
    return result


@app.get("/v1/stop/nearby")
def nearby_stops(lat: float, lon: float):
    _ensure_data_ready()
    stops = gtfs_service.nearest_stops(lat=lat, lon=lon, limit=8)
    if not stops:
        # fallback suave para no romper frontend si aun no se refrescaron datos
        stops = [
            {"stop_id": "EMT_1001", "name": "Alameda Principal", "distance_m": 180, "lat": lat, "lon": lon},
            {"stop_id": "EMT_1002", "name": "Plaza de la Marina", "distance_m": 320, "lat": lat, "lon": lon},
        ]
    return {"origin": {"lat": lat, "lon": lon}, "stops": stops}


@app.get("/v1/stop/search")
def search_stops(q: str):
    _ensure_data_ready()
    return {"query": q, "stops": gtfs_service.search_stops(q, limit=10)}


@app.get("/v1/eta")
def eta(stop_id: str, route_id: str):
    """ETA en minutos usando datos reales: realtime (posición de vehículos) + distancia a parada; fallback a horario."""
    _ensure_data_ready()
    result = compute_eta(
        stop_id=stop_id,
        route_id=route_id,
        gtfs_service=gtfs_service,
        realtime_service=realtime_service,
        realtime_last_updated=realtime_service.last_updated,
    )
    # Respuesta compatible: campos opcionales (ej. distance_m) no rompen clientes
    return {
        "stop_id": result["stop_id"],
        "route_id": result["route_id"],
        "eta_minutes": result["eta_minutes"],
        "confidence": result["confidence"],
        "source": result["source"],
        "realtime_last_updated": result["realtime_last_updated"],
    }


@app.get("/v1/route/{route_id}/eta-nearby")
def eta_nearby(route_id: str, lat: float, lon: float):
    """Demo: siguiente bus cercano por línea. Paradas cercanas a (lat, lon) servidas por la ruta, con ETA."""
    return eta_nearby_for_route(
        route_id=route_id,
        lat=lat,
        lon=lon,
        gtfs_service=gtfs_service,
        realtime_service=realtime_service,
    )


@app.post("/v1/route/plan", response_model=RoutePlanResponse)
def plan_route(payload: RoutePlanRequest):
    _ensure_data_ready()
    if not payload.destination_text.strip():
        raise HTTPException(status_code=400, detail="destination_text es obligatorio")

    nearby_origin = gtfs_service.nearest_stops(payload.origin_lat, payload.origin_lon, limit=5)
    if not nearby_origin:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "outside_service_area",
                "message": "No hay paradas EMT cercanas para tu ubicación.",
            },
        )
    nearest_distance_m = int(nearby_origin[0].get("distance_m", 999999))
    if nearest_distance_m > MAX_SERVICE_AREA_DISTANCE_M:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "outside_service_area",
                "message": (
                    "Estás fuera del área EMT Málaga. "
                    "Acércate a Málaga ciudad para calcular rutas de bus urbano."
                ),
                "nearest_stop_distance_m": nearest_distance_m,
            },
        )

    destination_matches = gtfs_service.search_stops(payload.destination_text, limit=8)

    computed_options = []
    for o in nearby_origin[:3]:
        for d in destination_matches[:4]:
            if o["stop_id"] == d["stop_id"]:
                continue
            trips = gtfs_service.find_trip_options(o["stop_id"], d["stop_id"], limit=2)
            for t in trips:
                walk_to_origin_min = max(1, round(o.get("distance_m", 200) / 80))
                walk_after_min = 3
                wait_min = _wait_eta_minutes(o["stop_id"], str(t["line"]))
                total_minutes = walk_to_origin_min + wait_min + t["duration_minutes"] + walk_after_min
                option = RouteOption(
                    route_id=t["trip_id"],
                    line=str(t["line"]),
                    origin_stop=o["name"],
                    destination_stop=d["name"],
                    total_minutes=total_minutes,
                    confidence="media" if realtime_service.positions else "baja",
                    wait_eta_minutes=wait_min,
                    walk_to_origin_distance_m=int(o.get("distance_m", 200)),
                    walk_to_origin_duration_min=walk_to_origin_min,
                    steps=[
                        RouteStep(
                            order=1,
                            step_type="walk",
                            instruction=f"Camina {o.get('distance_m', 200)} metros a {o['name']}.",
                            eta_minutes=walk_to_origin_min,
                        ),
                        RouteStep(
                            order=2,
                            step_type="wait",
                            instruction=(
                                f"Espera la línea {t['line']} "
                                f"({t['headsign'] or 'dirección principal'}). Llega aprox. en {wait_min} min."
                            ),
                            eta_minutes=wait_min,
                        ),
                        RouteStep(
                            order=3,
                            step_type="ride",
                            instruction=(
                                f"Sube al bus {t['line']} y bájate en {d['name']} "
                                f"(~{t['stops_between']} paradas)."
                            ),
                            eta_minutes=t["duration_minutes"],
                        ),
                        RouteStep(
                            order=4,
                            step_type="walk",
                            instruction=f"Camina hasta tu destino final ({payload.destination_text}).",
                            eta_minutes=walk_after_min,
                        ),
                    ],
                )
                computed_options.append(option)

    if computed_options:
        computed_options.sort(key=lambda x: x.total_minutes)
        top = computed_options[:3]
        top[0] = _enrich_walk_to_origin(top[0], payload.origin_lat, payload.origin_lon)
        return RoutePlanResponse(recommended_route_id=top[0].route_id, route_options=top)

    # Segundo intento: buscar destino por texto en paradas aguas abajo del origen.
    for o in nearby_origin[:4]:
        suggested = gtfs_service.suggest_trip_by_destination_text(
            o["stop_id"], payload.destination_text, limit=2
        )
        if not suggested:
            continue
        smart_options = []
        for t in suggested:
            walk_to_origin_min = max(1, round(o.get("distance_m", 200) / 80))
            wait_min = _wait_eta_minutes(o["stop_id"], str(t["line"]))
            walk_after_min = 3
            total_minutes = walk_to_origin_min + wait_min + t["duration_minutes"] + walk_after_min
            smart_options.append(
                RouteOption(
                    route_id=t["trip_id"],
                    line=str(t["line"]),
                    origin_stop=o["name"],
                    destination_stop=t.get("destination_stop_name") or payload.destination_text,
                    total_minutes=total_minutes,
                    confidence="media" if realtime_service.positions else "baja",
                    wait_eta_minutes=wait_min,
                    walk_to_origin_distance_m=int(o.get("distance_m", 200)),
                    walk_to_origin_duration_min=walk_to_origin_min,
                    steps=[
                        RouteStep(
                            order=1,
                            step_type="walk",
                            instruction=f"Camina {o.get('distance_m', 200)} metros a {o['name']}.",
                            eta_minutes=walk_to_origin_min,
                        ),
                        RouteStep(
                            order=2,
                            step_type="wait",
                            instruction=(
                                f"Espera la línea {t['line']} "
                                f"({t['headsign'] or 'dirección principal'}). Llega aprox. en {wait_min} min."
                            ),
                            eta_minutes=wait_min,
                        ),
                        RouteStep(
                            order=3,
                            step_type="ride",
                            instruction=(
                                f"Sube al bus {t['line']} y bájate en "
                                f"{t.get('destination_stop_name') or payload.destination_text} "
                                f"(~{t['stops_between']} paradas)."
                            ),
                            eta_minutes=t["duration_minutes"],
                        ),
                        RouteStep(
                            order=4,
                            step_type="walk",
                            instruction=f"Camina hasta tu destino final ({payload.destination_text}).",
                            eta_minutes=walk_after_min,
                        ),
                    ],
                )
            )
        if smart_options:
            smart_options.sort(key=lambda x: x.total_minutes)
            smart_options[0] = _enrich_walk_to_origin(
                smart_options[0], payload.origin_lat, payload.origin_lon
            )
            return RoutePlanResponse(
                recommended_route_id=smart_options[0].route_id,
                route_options=smart_options[:3],
            )

    # Tercer intento: viaje real desde origen aunque el destino no case perfecto.
    preferred_ids = [d["stop_id"] for d in destination_matches]
    for o in nearby_origin[:3]:
        basic = gtfs_service.suggest_basic_trip(o["stop_id"], preferred_stop_ids=preferred_ids, limit=2)
        if not basic:
            continue
        real_options = []
        for t in basic:
            walk_to_origin_min = max(1, round(o.get("distance_m", 200) / 80))
            wait_min = _wait_eta_minutes(o["stop_id"], str(t["line"]))
            walk_after_min = 4
            total_minutes = walk_to_origin_min + wait_min + t["duration_minutes"] + walk_after_min
            real_options.append(
                RouteOption(
                    route_id=t["trip_id"],
                    line=str(t["line"]),
                    origin_stop=o["name"],
                    destination_stop=t.get("destination_stop_name") or payload.destination_text,
                    total_minutes=total_minutes,
                    confidence="media" if realtime_service.positions else "baja",
                    wait_eta_minutes=wait_min,
                    walk_to_origin_distance_m=int(o.get("distance_m", 200)),
                    walk_to_origin_duration_min=walk_to_origin_min,
                    steps=[
                        RouteStep(
                            order=1,
                            step_type="walk",
                            instruction=f"Camina {o.get('distance_m', 200)} metros a {o['name']}.",
                            eta_minutes=walk_to_origin_min,
                        ),
                        RouteStep(
                            order=2,
                            step_type="wait",
                            instruction=(
                                f"Espera la línea {t['line']} "
                                f"({t['headsign'] or 'dirección principal'}). Llega aprox. en {wait_min} min."
                            ),
                            eta_minutes=wait_min,
                        ),
                        RouteStep(
                            order=3,
                            step_type="ride",
                            instruction=(
                                f"Sube al bus {t['line']} y baja en "
                                f"{t.get('destination_stop_name') or 'la parada indicada'} "
                                f"(~{t['stops_between']} paradas)."
                            ),
                            eta_minutes=t["duration_minutes"],
                        ),
                        RouteStep(
                            order=4,
                            step_type="walk",
                            instruction=f"Camina hasta {payload.destination_text}.",
                            eta_minutes=walk_after_min,
                        ),
                    ],
                )
            )
        if real_options:
            real_options.sort(key=lambda x: x.total_minutes)
            real_options[0] = _enrich_walk_to_origin(real_options[0], payload.origin_lat, payload.origin_lon)
            return RoutePlanResponse(
                recommended_route_id=real_options[0].route_id,
                route_options=real_options[:3],
            )

    # Sin fallback "inventado": devolver vacío para que UI lo comunique correctamente.
    return RoutePlanResponse(recommended_route_id="", route_options=[])


class JourneyStartRequest(BaseModel):
    route_id: str
    lang: str = "es"


@app.post("/v1/journey/start")
def start_journey(payload: JourneyStartRequest):
    journey_id = str(uuid4())
    _journey_state[journey_id] = {"step_index": 0, "route_id": payload.route_id, "lang": payload.lang}
    return {"journey_id": journey_id, "status": "started"}


@app.get("/v1/journey/{journey_id}/next-step")
def next_step(journey_id: str):
    state = _journey_state.get(journey_id)
    if not state:
        return {"error": "journey_not_found"}

    scripted_steps = [
        {
            "status": "walk",
            "step_type": "walk",
            "message": "Camina a la parada de salida.",
            "instruction": "Camina a la parada de salida.",
        },
        {
            "status": "wait",
            "step_type": "wait",
            "message": "Espera el bus de tu línea.",
            "instruction": "Espera el bus de tu línea. Te avisaremos cuando llegue.",
        },
        {
            "status": "ride",
            "step_type": "ride",
            "message": "Sube al bus y continúa el trayecto.",
            "instruction": "Sube al bus y sigue hasta la parada indicada.",
        },
        {
            "status": "alight",
            "step_type": "alight",
            "message": "Bájate en la siguiente parada.",
            "instruction": "Bájate en la siguiente parada.",
        },
        {
            "status": "finish",
            "step_type": "finish",
            "message": "Has llegado. Camina 2 minutos hasta destino.",
            "instruction": "Has llegado. Camina unos minutos hasta el destino final.",
        },
    ]

    idx = state["step_index"]
    if idx >= len(scripted_steps):
        return {
            "status": "completed",
            "step_type": "finish",
            "message": "Trayecto finalizado.",
            "instruction": "Trayecto finalizado.",
        }

    step = scripted_steps[idx]
    state["step_index"] += 1
    return step
