import csv
import io
import json
import math
import os
import ssl
import unicodedata
import urllib.request
import zipfile
from datetime import datetime, timezone


DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
GTFS_ZIP_PATH = os.path.join(DATA_DIR, "google_transit.zip")
GTFS_CACHE_PATH = os.path.join(DATA_DIR, "gtfs_cache.json")

# Fuente abierta del Ayuntamiento de Malaga (dataset GTFS EMT)
DEFAULT_GTFS_URL = (
    "https://datosabiertos.malaga.eu/recursos/transporte/EMT/"
    "lineasYHorarios/google_transit.zip"
)


def _ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def _haversine_m(lat1, lon1, lat2, lon2):
    r = 6371000
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return r * c


def _normalize_text(text):
    if text is None:
        return ""
    s = str(text).strip().lower()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    cleaned = []
    for ch in s:
        if ch.isalnum() or ch.isspace():
            cleaned.append(ch)
        else:
            cleaned.append(" ")
    return " ".join("".join(cleaned).split())


def _tokenize(text):
    return [t for t in _normalize_text(text).split() if len(t) >= 2]


class GTFSService:
    def __init__(self):
        self.stops = []
        self.routes = []
        self.trips = []
        self.stop_times = []
        self.last_updated = None
        self.stops_by_id = {}
        self.routes_by_id = {}
        self.trips_by_id = {}
        self.trip_stop_times = {}
        _ensure_data_dir()
        self.load_cache()

    def download_gtfs(self, url=DEFAULT_GTFS_URL):
        _ensure_data_dir()
        with _urlopen_with_ssl_fallback(url, timeout=30) as response:
            data = response.read()
        with open(GTFS_ZIP_PATH, "wb") as f:
            f.write(data)
        return len(data)

    def ingest_zip(self, zip_path=GTFS_ZIP_PATH):
        if not os.path.exists(zip_path):
            raise FileNotFoundError("No existe el zip GTFS")

        with zipfile.ZipFile(zip_path, "r") as zf:
            self.stops = self._read_csv_from_zip_multi(
                zf, ["stops.txt", "stops.csv"], required=["stop_id", "stop_name", "stop_lat", "stop_lon"]
            )
            self.routes = self._read_csv_from_zip_multi(
                zf, ["routes.txt", "routes.csv"], required=["route_id", "route_short_name"]
            )
            self.trips = self._read_csv_from_zip_multi(
                zf, ["trips.txt", "trips.csv"], required=["route_id", "trip_id"]
            )
            self.stop_times = self._read_csv_from_zip_multi(
                zf, ["stop_times.txt", "stop_times.csv"], required=["trip_id", "stop_id", "stop_sequence"]
            )

        self.last_updated = datetime.now(timezone.utc).isoformat()
        self._build_indexes()
        self.save_cache()

    def refresh(self, url=DEFAULT_GTFS_URL):
        self.download_gtfs(url=url)
        self.ingest_zip()
        return {
            "stops": len(self.stops),
            "routes": len(self.routes),
            "trips": len(self.trips),
            "stop_times": len(self.stop_times),
            "last_updated": self.last_updated,
        }

    def nearest_stops(self, lat, lon, limit=5):
        if not self.stops:
            return []
        enriched = []
        for stop in self.stops:
            try:
                s_lat = float(stop["stop_lat"])
                s_lon = float(stop["stop_lon"])
            except (TypeError, ValueError):
                continue
            distance = _haversine_m(lat, lon, s_lat, s_lon)
            enriched.append(
                {
                    "stop_id": stop["stop_id"],
                    "name": stop["stop_name"],
                    "lat": s_lat,
                    "lon": s_lon,
                    "distance_m": round(distance),
                }
            )
        enriched.sort(key=lambda x: x["distance_m"])
        return enriched[:limit]

    def search_stops(self, query, limit=8):
        """Busca paradas por nombre."""
        if not query or not self.stops:
            return []
        q = _normalize_text(query)
        if not q:
            return []
        q_tokens = set(_tokenize(q))
        matches = []
        for stop in self.stops:
            original_name = stop.get("stop_name") or ""
            name = _normalize_text(original_name)
            if not name:
                continue

            score = None
            if q == name:
                score = 1000
            elif name.startswith(q):
                score = 900 - len(name)
            elif q in name:
                score = 800 - len(name)
            else:
                name_tokens = set(_tokenize(name))
                overlap = len(q_tokens.intersection(name_tokens))
                if overlap > 0:
                    score = 600 + overlap * 20 - len(name)

            if score is not None:
                matches.append(
                    {
                        "stop_id": stop["stop_id"],
                        "name": original_name,
                        "lat": float(stop["stop_lat"]),
                        "lon": float(stop["stop_lon"]),
                        "_score": score,
                    }
                )
        matches.sort(key=lambda s: (-s["_score"], len(s["name"])))
        return [{k: v for k, v in s.items() if k != "_score"} for s in matches[:limit]]

    def find_trip_options(self, origin_stop_id, destination_stop_id, limit=5):
        """Encuentra viajes que pasan por origen y destino en el orden correcto."""
        if not origin_stop_id or not destination_stop_id:
            return []
        if origin_stop_id == destination_stop_id:
            return []

        options = []
        for trip_id, stop_rows in self.trip_stop_times.items():
            origin_row = None
            dest_row = None
            for row in stop_rows:
                sid = row.get("stop_id")
                if sid == origin_stop_id and origin_row is None:
                    origin_row = row
                if sid == destination_stop_id and dest_row is None:
                    dest_row = row
            if not origin_row or not dest_row:
                continue

            try:
                origin_seq = int(origin_row["stop_sequence"])
                dest_seq = int(dest_row["stop_sequence"])
            except (TypeError, ValueError):
                continue
            if dest_seq <= origin_seq:
                continue

            trip = self.trips_by_id.get(trip_id, {})
            route = self.routes_by_id.get(trip.get("route_id"), {})

            dep = origin_row.get("departure_time") or origin_row.get("arrival_time")
            arr = dest_row.get("arrival_time") or dest_row.get("departure_time")
            duration = _duration_minutes(dep, arr)
            if duration is None:
                duration = max(dest_seq - origin_seq, 5)

            options.append(
                {
                    "trip_id": trip_id,
                    "route_id": trip.get("route_id"),
                    "line": route.get("route_short_name") or route.get("route_id") or "?",
                    "route_long_name": route.get("route_long_name") or "",
                    "headsign": trip.get("trip_headsign") or "",
                    "origin_stop_id": origin_stop_id,
                    "destination_stop_id": destination_stop_id,
                    "departure_time": dep,
                    "arrival_time": arr,
                    "stops_between": dest_seq - origin_seq,
                    "duration_minutes": int(duration),
                }
            )

        options.sort(key=lambda x: (x["duration_minutes"], x["stops_between"]))
        return options[:limit]

    def suggest_trip_by_destination_text(self, origin_stop_id, destination_query, limit=3):
        """
        Busca viajes que salgan de una parada origen y tengan mas adelante una parada
        cuyo nombre coincida por texto con destination_query.
        """
        if not origin_stop_id or not destination_query:
            return []
        q = _normalize_text(destination_query)
        if not q:
            return []
        q_tokens = set(_tokenize(q))

        options = []
        for trip_id, stop_rows in self.trip_stop_times.items():
            origin_idx = None
            for idx, row in enumerate(stop_rows):
                if row.get("stop_id") == origin_stop_id:
                    origin_idx = idx
                    break
            if origin_idx is None:
                continue

            for j in range(origin_idx + 1, len(stop_rows)):
                dst_row = stop_rows[j]
                dst_id = dst_row.get("stop_id")
                dst_stop = self.stops_by_id.get(dst_id, {})
                dst_name = _normalize_text(dst_stop.get("stop_name") or "")
                if not dst_name:
                    continue

                text_match = q in dst_name
                token_overlap = len(q_tokens.intersection(set(_tokenize(dst_name)))) > 0
                if not text_match and not token_overlap:
                    continue

                origin_row = stop_rows[origin_idx]
                trip = self.trips_by_id.get(trip_id, {})
                route = self.routes_by_id.get(trip.get("route_id"), {})
                dep = origin_row.get("departure_time") or origin_row.get("arrival_time")
                arr = dst_row.get("arrival_time") or dst_row.get("departure_time")
                duration = _duration_minutes(dep, arr)
                if duration is None:
                    seq_o = _to_int(origin_row.get("stop_sequence"), default=1)
                    seq_d = _to_int(dst_row.get("stop_sequence"), default=seq_o + 1)
                    duration = max(seq_d - seq_o, 5)
                options.append(
                    {
                        "trip_id": trip_id,
                        "route_id": trip.get("route_id"),
                        "line": route.get("route_short_name") or route.get("route_id") or "?",
                        "route_long_name": route.get("route_long_name") or "",
                        "headsign": trip.get("trip_headsign") or "",
                        "origin_stop_id": origin_stop_id,
                        "destination_stop_id": dst_id,
                        "destination_stop_name": dst_stop.get("stop_name") or "",
                        "departure_time": dep,
                        "arrival_time": arr,
                        "stops_between": max(1, j - origin_idx),
                        "duration_minutes": int(duration),
                    }
                )
                break

        options.sort(key=lambda x: (x["duration_minutes"], x["stops_between"]))
        return options[:limit]

    def suggest_basic_trip(self, origin_stop_id, preferred_stop_ids=None, limit=3):
        """
        Devuelve opciones reales de viaje desde origen aunque no haya match textual.
        Útil como fallback inteligente para evitar rutas totalmente mock.
        """
        if not origin_stop_id:
            return []
        preferred_stop_ids = set(preferred_stop_ids or [])
        options = []

        for trip_id, stop_rows in self.trip_stop_times.items():
            origin_idx = None
            for idx, row in enumerate(stop_rows):
                if row.get("stop_id") == origin_stop_id:
                    origin_idx = idx
                    break
            if origin_idx is None:
                continue
            if origin_idx + 1 >= len(stop_rows):
                continue

            # Buscar destino preferido aguas abajo
            chosen_idx = None
            for j in range(origin_idx + 1, len(stop_rows)):
                if stop_rows[j].get("stop_id") in preferred_stop_ids:
                    chosen_idx = j
                    break

            # Si no hay preferido, usar una parada razonable unas cuantas posiciones adelante
            if chosen_idx is None:
                chosen_idx = min(origin_idx + 8, len(stop_rows) - 1)
                if chosen_idx <= origin_idx:
                    continue

            dst_row = stop_rows[chosen_idx]
            dst_id = dst_row.get("stop_id")
            dst_stop = self.stops_by_id.get(dst_id, {})
            origin_row = stop_rows[origin_idx]
            trip = self.trips_by_id.get(trip_id, {})
            route = self.routes_by_id.get(trip.get("route_id"), {})
            dep = origin_row.get("departure_time") or origin_row.get("arrival_time")
            arr = dst_row.get("arrival_time") or dst_row.get("departure_time")
            duration = _duration_minutes(dep, arr)
            if duration is None:
                duration = max(1, chosen_idx - origin_idx)

            options.append(
                {
                    "trip_id": trip_id,
                    "route_id": trip.get("route_id"),
                    "line": route.get("route_short_name") or route.get("route_id") or "?",
                    "route_long_name": route.get("route_long_name") or "",
                    "headsign": trip.get("trip_headsign") or "",
                    "origin_stop_id": origin_stop_id,
                    "destination_stop_id": dst_id,
                    "destination_stop_name": dst_stop.get("stop_name") or "",
                    "departure_time": dep,
                    "arrival_time": arr,
                    "stops_between": max(1, chosen_idx - origin_idx),
                    "duration_minutes": int(duration),
                    "is_preferred_destination": dst_id in preferred_stop_ids,
                }
            )

        options.sort(
            key=lambda x: (
                0 if x.get("is_preferred_destination") else 1,
                x["duration_minutes"],
                x["stops_between"],
            )
        )
        return options[:limit]

    def get_route_by_id(self, route_id):
        for r in self.routes:
            if r.get("route_id") == route_id:
                return r
        return None

    def stops_served_by_route(self, route_id):
        """Devuelve set de stop_id por los que pasa la ruta (para filtrar paradas en eta-nearby)."""
        norm = _normalize_route_id(route_id)
        stop_ids = set()
        for trip in self.trips:
            if _normalize_route_id(trip.get("route_id")) != norm:
                continue
            tid = trip.get("trip_id")
            for row in self.trip_stop_times.get(tid, []):
                sid = row.get("stop_id")
                if sid:
                    stop_ids.add(sid)
        return stop_ids

    def get_stop_coords(self, stop_id):
        """Devuelve (lat, lon) de la parada o None si no existe."""
        stop = self.stops_by_id.get(stop_id) if stop_id else None
        if not stop:
            return None
        try:
            return float(stop["stop_lat"]), float(stop["stop_lon"])
        except (TypeError, ValueError, KeyError):
            return None

    def save_cache(self):
        payload = {
            "stops": self.stops,
            "routes": self.routes,
            "trips": self.trips,
            "stop_times": self.stop_times,
            "last_updated": self.last_updated,
        }
        with open(GTFS_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)

    def load_cache(self):
        if not os.path.exists(GTFS_CACHE_PATH):
            return
        with open(GTFS_CACHE_PATH, "r", encoding="utf-8") as f:
            payload = json.load(f)
        self.stops = payload.get("stops", [])
        self.routes = payload.get("routes", [])
        self.trips = payload.get("trips", [])
        self.stop_times = payload.get("stop_times", [])
        self.last_updated = payload.get("last_updated")
        self._build_indexes()

    def _build_indexes(self):
        self.stops_by_id = {s.get("stop_id"): s for s in self.stops if s.get("stop_id")}
        self.routes_by_id = {r.get("route_id"): r for r in self.routes if r.get("route_id")}
        self.trips_by_id = {t.get("trip_id"): t for t in self.trips if t.get("trip_id")}

        trip_map = {}
        for row in self.stop_times:
            tid = row.get("trip_id")
            if not tid:
                continue
            trip_map.setdefault(tid, []).append(row)
        for tid, rows in trip_map.items():
            rows.sort(key=lambda r: _to_int(r.get("stop_sequence"), default=999999))
        self.trip_stop_times = trip_map

    @staticmethod
    def _read_csv_from_zip(zf, file_name, required=None):
        if file_name not in zf.namelist():
            return []
        raw = zf.read(file_name)
        text = raw.decode("utf-8-sig", errors="replace")
        reader = csv.DictReader(io.StringIO(text))
        rows = []
        required = required or []
        for row in reader:
            valid = all(row.get(k) not in (None, "") for k in required)
            if not valid:
                continue
            rows.append(row)
        return rows

    @staticmethod
    def _read_csv_from_zip_multi(zf, file_names, required=None):
        for file_name in file_names:
            if file_name in zf.namelist():
                return GTFSService._read_csv_from_zip(zf, file_name, required=required)
        return []


def _to_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_route_id(route_id):
    """Normaliza identificador de línea para comparar GTFS y realtime (ej. '1.0' -> '1')."""
    if route_id is None:
        return ""
    s = str(route_id).strip()
    if not s:
        return ""
    try:
        return str(int(float(s)))
    except (TypeError, ValueError):
        return s


def _time_to_seconds(hhmmss):
    if not hhmmss or ":" not in hhmmss:
        return None
    parts = hhmmss.split(":")
    if len(parts) != 3:
        return None
    try:
        h = int(parts[0])
        m = int(parts[1])
        s = int(parts[2])
    except ValueError:
        return None
    return h * 3600 + m * 60 + s


def _duration_minutes(dep, arr):
    dep_s = _time_to_seconds(dep)
    arr_s = _time_to_seconds(arr)
    if dep_s is None or arr_s is None:
        return None
    diff = arr_s - dep_s
    if diff < 0:
        return None
    return max(1, round(diff / 60))


def _urlopen_with_ssl_fallback(url, timeout=30):
    """Abre URL con fallback para entornos Windows con certificados corporativos."""
    try:
        return urllib.request.urlopen(url, timeout=timeout)
    except ssl.SSLError:
        ctx = ssl._create_unverified_context()
        return urllib.request.urlopen(url, timeout=timeout, context=ctx)
