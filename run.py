import os

import uvicorn


if __name__ == "__main__":
    # En cloud (Render/Railway/Fly) el puerto llega por variable PORT.
    port = int(os.environ.get("PORT", "5070"))
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, reload=False)
