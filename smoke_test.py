"""
Prueba rápida de endpoints básicos del backend MalagaBus.
Ejecutar con el servidor levantado (ej. start_dev.bat o run.py en puerto 5070).
"""
import sys
import urllib.request
import urllib.error
import json

BASE = "http://localhost:5070"


def get(path: str) -> dict:
    req = urllib.request.Request(f"{BASE}{path}", method="GET")
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode())


def post(path: str, data: dict) -> dict:
    body = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


def main():
    ok = 0
    fail = 0

    # GET /v1/health
    try:
        out = get("/v1/health")
        assert out.get("status") == "ok", out
        print("[OK] GET /v1/health ->", out.get("service", ""), "gtfs_stops:", out.get("gtfs_stops"))
        ok += 1
    except Exception as e:
        print("[FAIL] GET /v1/health ->", e)
        fail += 1

    # GET /v1/data/status
    try:
        out = get("/v1/data/status")
        assert "gtfs" in out and "realtime" in out, out
        print("[OK] GET /v1/data/status -> gtfs.stops:", out["gtfs"].get("stops"))
        ok += 1
    except Exception as e:
        print("[FAIL] GET /v1/data/status ->", e)
        fail += 1

    # POST /v1/route/plan
    try:
        payload = {
            "origin_lat": 36.7213,
            "origin_lon": -4.4214,
            "destination_text": "Alameda",
            "lang": "es",
        }
        out = post("/v1/route/plan", payload)
        assert "recommended_route_id" in out and "route_options" in out, out
        print("[OK] POST /v1/route/plan -> recommended_route_id:", out.get("recommended_route_id"))
        ok += 1
    except Exception as e:
        print("[FAIL] POST /v1/route/plan ->", e)
        fail += 1

    print()
    print(f"Smoke test: {ok} OK, {fail} FAIL")
    sys.exit(1 if fail else 0)


if __name__ == "__main__":
    main()
