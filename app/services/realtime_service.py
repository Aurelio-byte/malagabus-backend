import json
import os
import ssl
import urllib.request
from datetime import datetime, timezone


DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
REALTIME_CACHE_PATH = os.path.join(DATA_DIR, "realtime_positions.json")

# Fuente abierta EMT Malaga (posiciones en tiempo real)
DEFAULT_REALTIME_URL = (
    "https://datosabiertos.malaga.eu/recursos/transporte/EMT/"
    "EMTlineasUbicaciones/lineasyubicaciones.geojson"
)


def _ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


class RealtimeService:
    def __init__(self):
        self.positions = []
        self.last_updated = None
        _ensure_data_dir()
        self.load_cache()

    def refresh(self, url=DEFAULT_REALTIME_URL):
        with _urlopen_with_ssl_fallback(url, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))

        if isinstance(payload, dict):
            features = payload.get("features", [])
        elif isinstance(payload, list):
            features = payload
        else:
            features = []
        parsed = []
        for f in features:
            geom = f.get("geometry", {})
            props = f.get("properties", {})
            coords = geom.get("coordinates", [])
            if len(coords) < 2:
                continue
            lon, lat = coords[0], coords[1]
            line = props.get("linea") or props.get("line") or props.get("LINEA")
            if line is None and "codLinea" in props:
                raw_line = props.get("codLinea")
                try:
                    line = str(int(float(raw_line))) if raw_line is not None else None
                except (TypeError, ValueError):
                    line = str(raw_line) if raw_line is not None else None
            vehicle = props.get("vehiculo") or props.get("vehicle") or props.get("VEHICULO") or props.get("codBus")
            parsed.append(
                {
                    "lat": lat,
                    "lon": lon,
                    "line": line,
                    "vehicle": vehicle,
                    "raw": props,
                }
            )

        self.positions = parsed
        self.last_updated = datetime.now(timezone.utc).isoformat()
        self.save_cache()
        return {"vehicles": len(parsed), "last_updated": self.last_updated}

    def save_cache(self):
        payload = {"positions": self.positions, "last_updated": self.last_updated}
        with open(REALTIME_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)

    def load_cache(self):
        if not os.path.exists(REALTIME_CACHE_PATH):
            return
        with open(REALTIME_CACHE_PATH, "r", encoding="utf-8") as f:
            payload = json.load(f)
        self.positions = payload.get("positions", [])
        self.last_updated = payload.get("last_updated")


def _urlopen_with_ssl_fallback(url, timeout=20):
    try:
        return urllib.request.urlopen(url, timeout=timeout)
    except ssl.SSLError:
        ctx = ssl._create_unverified_context()
        return urllib.request.urlopen(url, timeout=timeout, context=ctx)
