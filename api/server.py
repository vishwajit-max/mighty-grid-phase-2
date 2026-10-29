import asyncio
import json
import os
import re
import shutil
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
    if not broadcaster.loop:
        broadcaster.loop = asyncio.get_running_loop()
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
    """Return recordings newest-first using the exported recording.json timestamp.

    Session folder names are random IDs, so sorting paths lexicographically does
    not represent recording time. The recording.json file is written when the
    session is exported, making its filesystem mtime the best available time
    source when older session JSON files have no timestamp field.
    """
    recordings_root = REC_DIR / "recordings"
    found = []
    if recordings_root.exists():
        for rec_file in recordings_root.rglob("recording.json"):
            try:
                data = json.loads(rec_file.read_text(encoding="utf-8"))
                recorded_at = rec_file.stat().st_mtime
                found.append({
                    "id": data.get("session_id", rec_file.parent.name),
                    "path": str(rec_file),
                    "label": f"{rec_file.parent.name} ({data.get('total_steps', 0)} steps)",
                    "source_serial": data.get("source_device_serial", "Unknown"),
                    "steps": data.get("total_steps", 0),
                    "recorded_at": recorded_at,
                })
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                # Ignore incomplete or unreadable session exports without
                # preventing the remaining recordings from appearing.
                continue

    # Newest session first. The UI applies the same stable sort as a safeguard.
    found.sort(key=lambda recording: recording["recorded_at"], reverse=True)
    return {"recordings": found}


@app.delete("/api/recordings/{recording_id}")
def delete_recording(recording_id: str):
    """Delete one saved session directory, including its recording.json and crops."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", recording_id):
        raise HTTPException(status_code=400, detail="Invalid recording ID.")

    # Prevent removal of a file that the replay engine may still be reading.
    with STATE_LOCK:
        replay_proc = ACTIVE_STATE.get("replay_proc")
        if replay_proc is not None and replay_proc.poll() is None:
            raise HTTPException(
                status_code=409,
                detail="Stop the active replay before deleting a recording.",
            )

    recordings_root = (REC_DIR / "recordings").resolve()
    if not recordings_root.exists():
        raise HTTPException(status_code=404, detail="No recordings directory exists.")

    matches = []
    for rec_file in recordings_root.rglob("recording.json"):
        try:
            data = json.loads(rec_file.read_text(encoding="utf-8"))
            record_id = str(data.get("session_id") or rec_file.parent.name)
            if record_id == recording_id:
                matches.append(rec_file.resolve())
        except (OSError, json.JSONDecodeError):
            continue

    if not matches:
        raise HTTPException(status_code=404, detail="Recording not found.")
    if len(matches) > 1:
        raise HTTPException(
            status_code=409,
            detail="Multiple recordings share this ID; deletion was cancelled for safety.",
        )

    recording_file = matches[0]
    session_dir = recording_file.parent.resolve()
    # Strict containment checks: never allow deletion of the recordings root or
    # anything outside it, even if a recording or symlink was tampered with.
    if session_dir == recordings_root or recordings_root not in session_dir.parents:
        raise HTTPException(status_code=403, detail="Unsafe recording path; deletion refused.")
    if recording_file.name != "recording.json" or not recording_file.is_file():
        raise HTTPException(status_code=400, detail="Invalid recording file.")

    try:
        shutil.rmtree(session_dir)
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Could not delete session directory: {exc}",
        ) from exc

    print(f"[+] Deleted recording session {recording_id} ({session_dir.name}).")
    return {"status": "DELETED", "id": recording_id, "deleted_directory": session_dir.name}


# ---------------------------------------------------------------------
# RECORD / REPLAY / STOP EXECUTION ROUTES
# ---------------------------------------------------------------------
class RecordRequest(BaseModel):
    serial: str
    persona: str = "average"
    seed: Optional[int] = None


@app.post("/api/record/start")
async def start_recorder(req: RecordRequest):
    ui_script = REC_DIR / "viewport_recorder_ui.py"
    if not ui_script.exists():
        raise HTTPException(status_code=404, detail="viewport_recorder_ui.py not found")

    if req.persona not in [*personas.ARCHETYPE_NAMES, "random"]:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown persona '{req.persona}'. Available: {', '.join([*personas.ARCHETYPE_NAMES, 'random'])}",
        )

    loop = asyncio.get_running_loop()
    seed_note = f", seed {req.seed}" if req.seed is not None else ""
    print(
        f"[*] Spawning Scrcpy Recorder on {req.serial} "
        f"with {req.persona.title()} gesture profile{seed_note}..."
    )

    cmd = [
        sys.executable, str(ui_script), req.serial,
        "--persona", req.persona,
    ]
    if req.seed is not None:
        cmd.extend(["--seed", str(req.seed)])

    proc = subprocess.Popen(cmd, cwd=str(REC_DIR))
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
    return {
        "status": "RECORDING_STARTED",
        "serial": req.serial,
        "persona": req.persona,
        "seed": req.seed,
    }


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

            print(
                f"[*] Replay entrypoint: {replay_script.resolve()} | "
                f"exists={replay_script.is_file()}",
                flush=True,
            )
            print(
                f"[*] Replay target: recording={Path(req.recording_path).resolve()} | "
                f"devices={','.join(req.serials)} | persona={req.persona}",
                flush=True,
            )
            if not replay_script.is_file():
                raise FileNotFoundError(f"Replay engine not found: {replay_script}")

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

            # Popen.stdout is typed as optional; make the PIPE contract explicit
            # before iterating so a missing stream is reported as a fatal error.
            if proc.stdout is None:
                raise RuntimeError("Replay subprocess stdout pipe was not created.")
            output_stream = proc.stdout

            # Capture evidence that the expected engine really ran and emitted a report.
            saw_engine_build = False
            saw_replay_report = False
            saw_step_activity = False
            saw_fleet_pass = False
            saw_fleet_failure = False
            for line in output_stream:
                msg = line.strip()
                if msg:
                    if "Replay engine build:" in msg:
                        saw_engine_build = True
                    if "=== Replay Report [" in msg:
                        saw_replay_report = True
                    if "[+] FLEET REPLAY PASSED:" in msg:
                        saw_fleet_pass = True
                    if "[!] FLEET REPLAY FAILED:" in msg:
                        saw_fleet_failure = True
                    if "Step " in msg and (" started." in msg or " status=" in msg):
                        saw_step_activity = True
                    print(msg, flush=True)

            proc.wait()

            with STATE_LOCK:
                was_aborted = ACTIVE_STATE["aborted"]

            if was_aborted:
                print("[!] Replay aborted by user.", flush=True)
            elif saw_fleet_failure:
                print("[!] Fleet replay reported failed or unexecuted steps.", flush=True)
            elif not saw_engine_build:
                print(
                    "[!] Replay process ended without the expected engine build marker. "
                    "Check the logged entrypoint and restart the correct API backend.",
                    flush=True,
                )
            elif not saw_replay_report:
                print(
                    f"[!] Replay process exited without a per-device replay report "
                    f"(code={proc.returncode}, step_activity={saw_step_activity}). "
                    "The run is not considered successful.",
                    flush=True,
                )
            elif not saw_fleet_pass:
                print(
                    f"[!] Replay did not emit the required all-steps-passed marker "
                    f"(exit code={proc.returncode}). This run is NOT marked successful.",
                    flush=True,
                )
            elif proc.returncode == 0:
                print("[+] Fleet replay verified: all recorded steps passed.", flush=True)
            else:
                print(f"[!] Replay process exited with code {proc.returncode}; replay FAILED.", flush=True)

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
