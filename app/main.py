import os
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
    return {"query": q, "stops": gtfs_service.search_stops(q, limit=10)}


@app.get("/v1/eta")
def eta(stop_id: str, route_id: str):
    """ETA en minutos usando datos reales: realtime (posición de vehículos) + distancia a parada; fallback a horario."""
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
    if not payload.destination_text.strip():
        raise HTTPException(status_code=400, detail="destination_text es obligatorio")

    nearby_origin = gtfs_service.nearest_stops(payload.origin_lat, payload.origin_lon, limit=5)
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
                wait_min = 4
                total_minutes = walk_to_origin_min + wait_min + t["duration_minutes"] + walk_after_min
                option = RouteOption(
                    route_id=t["trip_id"],
                    line=str(t["line"]),
                    origin_stop=o["name"],
                    destination_stop=d["name"],
                    total_minutes=total_minutes,
                    confidence="media" if realtime_service.positions else "baja",
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
                            instruction=f"Espera la línea {t['line']} ({t['headsign'] or 'dirección principal'}).",
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
            wait_min = 4
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
                            instruction=f"Espera la línea {t['line']} ({t['headsign'] or 'dirección principal'}).",
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
            wait_min = 4
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
                            instruction=f"Espera la línea {t['line']} ({t['headsign'] or 'dirección principal'}).",
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
            return RoutePlanResponse(
                recommended_route_id=real_options[0].route_id,
                route_options=real_options[:3],
            )

    # Fallback controlado si no hay coincidencias reales suficientes
    origin_name = nearby_origin[0]["name"] if nearby_origin else "Parada cercana"
    fallback = RouteOption(
        route_id="fallback_route",
        line="L3",
        origin_stop=origin_name,
        destination_stop=payload.destination_text,
        total_minutes=24,
        confidence="baja",
        steps=[
            RouteStep(order=1, step_type="walk", instruction=f"Camina a la parada {origin_name}.", eta_minutes=3),
            RouteStep(order=2, step_type="wait", instruction="Espera el próximo bus disponible.", eta_minutes=5),
            RouteStep(order=3, step_type="ride", instruction="Sigue la guía y te avisaremos cuándo bajar.", eta_minutes=13),
            RouteStep(order=4, step_type="walk", instruction="Camina hasta el destino final.", eta_minutes=3),
        ],
    )
    return RoutePlanResponse(recommended_route_id=fallback.route_id, route_options=[fallback])


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
