#!/usr/bin/env python3
"""
api/server.py
Backend endpoint connecting your UI dropdowns/forms to the Dispatcher.
Run via: uvicorn api.server:app --reload --port 8000
"""

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional, Literal
from core.unified_dispatcher import UnifiedTelemetryRunner

app = FastAPI(title="MightyGrid Unified Testing Engine")

class TestRequest(BaseModel):
    engine: Literal["browser", "android"]
    
    # Browser Parameters
    url: Optional[str] = "https://google.com"
    proxy: Optional[str] = None
    retries: Optional[int] = 3
    
    # Android Parameters
    package: Optional[str] = "com.android.chrome"
    serial: Optional[str] = None
    persona: Optional[Literal["average", "restless", "careless", "speedy", "shaky"]] = "average"
    hard_reset: Optional[bool] = False
    silent: Optional[bool] = True

@app.post("/api/run-test")
async def trigger_testing_engine(payload: TestRequest):
    try:
        report = UnifiedTelemetryRunner.execute(
            selection=payload.engine,
            params=payload.dict()
        )
        return report
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Execution failed: {str(exc)}")

@app.get("/api/adb-devices")
def get_connected_devices():
    """Helper route so your UI can populate a dropdown of connected Android devices."""
    try:
        import subprocess
        out = subprocess.check_output(["adb", "devices"], text=True)
        devices = []
        for line in out.splitlines()[1:]:
            parts = line.strip().split("\t")
            if len(parts) == 2 and parts[1] == "device":
                devices.append(parts[0])
        return {"devices": devices}
    except Exception:
        return {"devices": []}