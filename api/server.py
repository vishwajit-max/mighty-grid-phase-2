import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

ROOT_DIR = Path(__file__).resolve().parent.parent
REC_DIR = ROOT_DIR / "recording_engine"
TELE_DIR = ROOT_DIR / "android_telemetry" / "telemetry"

for p in (str(REC_DIR), str(TELE_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import personas

app = FastAPI(title="MightyGrid Unified Studio API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------
# REAL-TIME WEBSOCKET LOG BROADCASTER
# ---------------------------------------------------------------------
class LogBroadcaster:
    def __init__(self):
        self.connections: List[WebSocket] = []
        self.loop = None
        self._orig_stdout = sys.stdout

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.connections.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.connections:
            self.connections.remove(ws)

    def write(self, text: str):
        self._orig_stdout.write(text)
        self._orig_stdout.flush()
        if text.strip() and self.loop and self.connections:
            for line in text.splitlines():
                if line.strip():
                    asyncio.run_coroutine_threadsafe(self.broadcast(line), self.loop)

    def flush(self):
        self._orig_stdout.flush()

    async def broadcast(self, message: str):
        dead = []
        for ws in list(self.connections):
            try:
                await ws.send_json({"type": "log", "message": message})
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


broadcaster = LogBroadcaster()

# Track active processes so they can be killed mid-run
ACTIVE_STATE = {
    "replay_proc": None,
    "viewers": [],
    "recorder_proc": None,
    "active_serials": [],
    "aborted": False,
}
STATE_LOCK = threading.Lock()


@app.on_event("startup")
async def on_startup():
    broadcaster.loop = asyncio.get_running_loop()
    sys.stdout = broadcaster
    try:
        subprocess.run(["adb", "start-server"], capture_output=True, timeout=15)
    except Exception:
        pass


@app.websocket("/ws/logs")
async def websocket_logs(ws: WebSocket):
    await broadcaster.connect(ws)
    await ws.send_json({"type": "log", "message": "[*] Connected to MightyGrid Telemetry Stream."})
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        broadcaster.disconnect(ws)


# ---------------------------------------------------------------------
# FLEET & PERSONA ENDPOINTS
# ---------------------------------------------------------------------
def _fetch_device_details_fast(serial: str) -> dict:
    script = (
        "getprop ro.product.brand; "
        "getprop ro.product.model; "
        "getprop ro.build.version.release; "
        "getprop ro.hardware"
    )
    try:
        res = subprocess.run(
            ["adb", "-s", serial, "shell", script],
            capture_output=True, text=True, timeout=4
        )
        lines = [line.strip() for line in res.stdout.splitlines()]
        brand = (lines[0] if len(lines) > 0 else "").capitalize()
        model = lines[1] if len(lines) > 1 else ""
        ver = (lines[2] if len(lines) > 2 and lines[2] else "14")
        soc = (lines[3] if len(lines) > 3 and lines[3] else "arm64")
        return {
            "serial": serial,
            "name": f"{brand} {model}".strip() or "Android Device",
            "android_version": ver,
            "soc": soc,
            "status": "ONLINE"
        }
    except Exception:
        return {
            "serial": serial,
            "name": "Android Device",
            "android_version": "14",
            "soc": "arm64",
            "status": "ONLINE"
        }


@app.get("/api/devices")
async def list_devices():
    devices = []
    try:
        res = await asyncio.to_thread(
            subprocess.run,
            ["adb", "devices"],
            capture_output=True,
            text=True,
            timeout=15
        )
        serials = [
            line.split("\t")[0].strip()
            for line in res.stdout.strip().splitlines()[1:]
            if "\tdevice" in line
        ]
        if serials:
            tasks = [asyncio.to_thread(_fetch_device_details_fast, s) for s in serials]
            devices = await asyncio.gather(*tasks)
    except Exception as e:
        print(f"[!] ADB Fleet Scan Error: {e}")

    return {"devices": list(devices)}


def _serialize_persona(p) -> dict:
    """Return a sampled persona plus the effective range for every trait."""
    ranges = personas.get_persona_ranges(p.archetype)
    precision = {
        "wpm": 1,
        "typo_rate": 4,
        "burstiness": 3,
        "scatter_px": 2,
        "fitts_precision": 3,
        "reaction_ms": 1,
        "dwell_scale": 3,
        "curve_bias": 3,
        "tremor_scale": 3,
        "flick_vigor": 3,
        "overshoot_rate": 4,
        "fatigue_rate": 4,
        "fling_bias": 3,
        "curiosity": 3,
        "patience": 3,
    }

    parameters = {}
    for trait, trait_range in ranges.items():
        parameters[trait] = {
            "value": round(float(getattr(p, trait)), precision.get(trait, 3)),
            "min": round(trait_range["min"], precision.get(trait, 3)),
            "max": round(trait_range["max"], precision.get(trait, 3)),
            "signed": trait_range.get("signed", False),
        }

    return {
        "name": p.name,
        "archetype": p.archetype,
        "seed": p.seed,
        # Legacy aliases retained for the existing four primary meters.
        "wpm": round(p.wpm, 1),
        "vigor": round(p.flick_vigor, 2),
        "tremor": round(p.tremor_scale, 2),
        "scatter": round(p.scatter_px, 1),
        "reaction_ms": round(p.reaction_ms, 0),
        "fling_bias": round(p.fling_bias, 2),
        "parameters": parameters,
    }


@app.get("/api/personas")
def list_personas():
    """Return one fresh, range-constrained sample for every persona on load."""
    out = {
        arch: _serialize_persona(personas.generate_persona(seed=None, archetype=arch))
        for arch in personas.ARCHETYPE_NAMES
    }
    out["random"] = _serialize_persona(
        personas.generate_persona(seed=None, archetype="random")
    )
    return {"personas": out}


@app.get("/api/personas/random")
def roll_random_persona():
    """Generate a new random archetype and a fresh, reproducible trait sample."""
    return _serialize_persona(
        personas.generate_persona(seed=None, archetype="random")
    )


@app.get("/api/personas/{archetype}")
def roll_persona(archetype: str):
    """Generate a fresh sample from the requested archetype's defined ranges."""
    if archetype not in personas.ARCHETYPE_NAMES:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown persona '{archetype}'. Available: {', '.join(personas.ARCHETYPE_NAMES)}, random",
        )
    return _serialize_persona(
        personas.generate_persona(seed=None, archetype=archetype)
    )


@app.get("/api/recordings")
def list_recordings():
    recordings_root = REC_DIR / "recordings"
    found = []
    if recordings_root.exists():
        for rec_file in sorted(recordings_root.rglob("recording.json"), reverse=True):
            try:
                data = json.loads(rec_file.read_text(encoding="utf-8"))
                found.append({
                    "id": data.get("session_id", rec_file.parent.name),
                    "path": str(rec_file),
                    "label": f"{rec_file.parent.name} ({data.get('total_steps', 0)} steps)",
                    "source_serial": data.get("source_device_serial", "Unknown"),
                    "steps": data.get("total_steps", 0)
                })
            except Exception:
                pass
    return {"recordings": found}


# ---------------------------------------------------------------------
# RECORD / REPLAY / STOP EXECUTION ROUTES
# ---------------------------------------------------------------------
class RecordRequest(BaseModel):
    serial: str


@app.post("/api/record/start")
async def start_recorder(req: RecordRequest):
    ui_script = REC_DIR / "viewport_recorder_ui.py"
    if not ui_script.exists():
        raise HTTPException(status_code=404, detail="viewport_recorder_ui.py not found")

    loop = asyncio.get_running_loop()
    print(f"[*] Spawning 60FPS Scrcpy Recorder Viewport on {req.serial}...")

    proc = subprocess.Popen([sys.executable, str(ui_script), req.serial], cwd=str(REC_DIR))
    with STATE_LOCK:
        ACTIVE_STATE["recorder_proc"] = proc

    def _watch_recorder():
        proc.wait()
        
        # Give the OS 500ms to flush the session directory and recording.json to disk
        time.sleep(0.5)
        
        with STATE_LOCK:
            ACTIVE_STATE["recorder_proc"] = None
        asyncio.run_coroutine_threadsafe(broadcaster.broadcast("__STATUS__:IDLE"), loop)

    threading.Thread(target=_watch_recorder, daemon=True).start()
    return {"status": "RECORDING_STARTED", "serial": req.serial}


class ReplayRequest(BaseModel):
    recording_path: str
    serials: List[str]
    persona: str = "average"
    seed: Optional[int] = None
    lockstep: bool = True


@app.post("/api/replay/start")
async def start_replay(req: ReplayRequest):
    if not req.serials:
        raise HTTPException(status_code=400, detail="No target devices selected.")
    if not Path(req.recording_path).exists():
        raise HTTPException(status_code=404, detail=f"Recording file not found: {req.recording_path}")

    loop = asyncio.get_running_loop()

    def _worker():
        viewers = []
        proc = None
        try:
            with STATE_LOCK:
                ACTIVE_STATE["aborted"] = False
                ACTIVE_STATE["active_serials"] = list(req.serials)

            asyncio.run_coroutine_threadsafe(
                broadcaster.broadcast("__STATUS__:REPLAYING"), loop
            )

            # 1. Spawn Scrcpy Live Monitors
            for serial in req.serials:
                try:
                    v_proc = subprocess.Popen(
                        [
                            "scrcpy", "-s", serial,
                            "--no-control",
                            "--max-fps=30",
                            "--window-title", f"Live Replay Monitor - {serial}"
                        ],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    viewers.append(v_proc)
                except Exception:
                    print(f"[!] Could not launch scrcpy viewer for {serial}.")

            with STATE_LOCK:
                ACTIVE_STATE["viewers"] = viewers

            # 2. Spawn replay_engine.py as an isolated subprocess so it can be terminated mid-run
            replay_script = REC_DIR / "replay_engine.py"
            cmd = [
                sys.executable, "-u", str(replay_script),
                str(req.recording_path),
                *req.serials,
                "--persona", req.persona,
            ]
            if req.seed is not None:
                cmd.extend(["--seed", str(req.seed)])
            if not req.lockstep:
                cmd.append("--no-lockstep")

            proc = subprocess.Popen(
                cmd,
                cwd=str(REC_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            with STATE_LOCK:
                ACTIVE_STATE["replay_proc"] = proc

            for line in iter(proc.stdout.readline, ""):
                if line.strip():
                    print(line.rstrip())

            proc.wait()

            with STATE_LOCK:
                was_aborted = ACTIVE_STATE["aborted"]

            if was_aborted:
                print("[!] Replay aborted by user.")
            elif proc.returncode == 0:
                print("[+] Fleet replay execution finished.")
            else:
                print(f"[!] Replay process exited with code {proc.returncode}.")

        except Exception as exc:
            print(f"[!] Fatal Replay Error: {exc}")
        finally:
            with STATE_LOCK:
                ACTIVE_STATE["replay_proc"] = None
                for v in ACTIVE_STATE["viewers"]:
                    try:
                        v.terminate()
                    except Exception:
                        pass
                ACTIVE_STATE["viewers"] = []
                ACTIVE_STATE["active_serials"] = []

            asyncio.run_coroutine_threadsafe(
                broadcaster.broadcast("__STATUS__:IDLE"), loop
            )

    threading.Thread(target=_worker, daemon=True).start()
    return {"status": "REPLAY_DISPATCHED"}


@app.post("/api/run/stop")
def stop_active_run():
    """Immediately kills any running Replay subprocess, Scrcpy viewers, and MaaTouch daemons."""
    killed_anything = False
    with STATE_LOCK:
        ACTIVE_STATE["aborted"] = True
        proc = ACTIVE_STATE.get("replay_proc")
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                killed_anything = True
            except Exception:
                pass

        rec_proc = ACTIVE_STATE.get("recorder_proc")
        if rec_proc and rec_proc.poll() is None:
            try:
                rec_proc.terminate()
                killed_anything = True
            except Exception:
                pass

        for v in ACTIVE_STATE.get("viewers", []):
            try:
                v.terminate()
            except Exception:
                pass
        ACTIVE_STATE["viewers"] = []
        serials = list(ACTIVE_STATE.get("active_serials", []))

    # Clean up on-device MaaTouch daemon so no touch remains held down
    for s in serials:
        try:
            subprocess.run(
                ["adb", "-s", s, "shell", "pkill -f com.shxyke.MaaTouch.App"],
                capture_output=True, timeout=3
            )
        except Exception:
            pass

    print("[!] STOP signal dispatched — terminating active fleet processes.")
    return {"status": "STOPPED", "killed": killed_anything}