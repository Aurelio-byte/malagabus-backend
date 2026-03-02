# MalagaBus Backend (MVP base)

Backend inicial para la app de guiado en bus en Málaga.

## Ejecutar local

1. Crear entorno virtual
2. Instalar dependencias:

```bash
pip install -r requirements.txt
```

3. Arrancar API:

```bash
uvicorn app.main:app --reload --port 5070
```

4. Probar docs:

- `http://localhost:5070/docs`

## Scripts de desarrollo y prueba

- **start_dev.bat** (Windows): arranque local todo-en-uno. Activa el entorno virtual si existe (`venv` o `.venv`), instala `requirements.txt` si faltan dependencias y lanza la API en el puerto 5070. Ejecutar desde la raíz del backend:  
  `start_dev.bat`

- **smoke_test.py**: prueba rápida de endpoints básicos (health, data/status, route/plan). El servidor debe estar levantado. Ejecutar:  
  `python smoke_test.py`  
  Devuelve código de salida 0 si todo pasa, 1 si algún endpoint falla.

## Endpoints

- `GET /v1/health`
- `GET /v1/data/status` — estado de carga GTFS y realtime
- `POST /v1/data/refresh` — refrescar datos GTFS y realtime
- `GET /v1/stop/nearby?lat=&lon=`
- `GET /v1/stop/search?q=`
- `GET /v1/eta?stop_id=&route_id=` — ETA en minutos (datos reales: realtime + distancia; fallback horario)
- `GET /v1/route/{route_id}/eta-nearby?lat=&lon=` — siguiente bus cercano por línea (demo)
- `POST /v1/route/plan`
- `POST /v1/journey/start`
- `GET /v1/journey/{journey_id}/next-step`

## Despliegue gratis (sin PC en la calle)

Para usar MalagaBus manana sin llevar Titan, necesitas una URL publica.

### Opcion recomendada: Render (free)

1. Sube `malagabus_backend` a un repo de GitHub.
2. En Render: **New + > Web Service > Connect repo**.
3. Render detecta `render.yaml` y aplica:
   - Build: `pip install -r requirements.txt`
   - Start: `python run.py`
   - Health: `/v1/health`
4. Cuando termine, abre:
   - `https://TU-SERVICIO.onrender.com/app`
5. En el movil, crea el icono desde esa URL (ya no depende del PC).

Notas:
- En plan free puede tardar unos segundos en "despertar" si estuvo inactiva.
- `run.py` ya usa `PORT` automaticamente para cloud.
