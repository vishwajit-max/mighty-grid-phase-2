"use client";

import React, { useEffect, useState, useRef } from "react";
import {
  Smartphone,
  RefreshCw,
  Disc,
  Play,
  Square,
  Terminal as TerminalIcon,
  Activity,
  Trash2,
  Check,
  Sparkles,
  Lock,
  Unlock,
  FolderOpen,
  Zap,
  Dices,
} from "lucide-react";

const API_BASE = "http://localhost:8000";
const WS_URL = "ws://localhost:8000/ws/logs";

interface Device {
  serial: string;
  name: string;
  android_version: string;
  soc: string;
  status: string;
}

interface PersonaParameter {
  value: number;
  min: number;
  max: number;
  signed?: boolean;
}

interface PersonaMetrics {
  name: string;
  archetype?: string;
  seed?: number;
  wpm: number;
  vigor: number;
  tremor: number;
  scatter: number;
  reaction_ms: number;
  fling_bias: number;
  parameters?: Record<string, PersonaParameter>;
}

const PERSONA_PARAMETER_ORDER = [
  "wpm",
  "typo_rate",
  "burstiness",
  "scatter_px",
  "fitts_precision",
  "reaction_ms",
  "dwell_scale",
  "curve_bias",
  "tremor_scale",
  "flick_vigor",
  "overshoot_rate",
  "fatigue_rate",
  "fling_bias",
  "curiosity",
  "patience",
];

const PERSONA_PARAMETER_META: Record<string, { label: string; unit?: string; percent?: boolean; decimals?: number }> = {
  wpm: { label: "Typing speed", unit: "WPM", decimals: 1 },
  typo_rate: { label: "Typo rate", percent: true, decimals: 1 },
  burstiness: { label: "Typing burstiness", unit: "×", decimals: 2 },
  scatter_px: { label: "Touch scatter", unit: "px", decimals: 2 },
  fitts_precision: { label: "Fitts precision", unit: "×", decimals: 2 },
  reaction_ms: { label: "Reaction time", unit: "ms", decimals: 0 },
  dwell_scale: { label: "Touch dwell scale", unit: "×", decimals: 2 },
  curve_bias: { label: "Gesture curve bias", decimals: 2 },
  tremor_scale: { label: "Micro-tremor", unit: "σ", decimals: 2 },
  flick_vigor: { label: "Flick vigor", unit: "×", decimals: 2 },
  overshoot_rate: { label: "Overshoot rate", percent: true, decimals: 1 },
  fatigue_rate: { label: "Fatigue rate", percent: true, decimals: 1 },
  fling_bias: { label: "Fling bias", decimals: 2 },
  curiosity: { label: "Curiosity", percent: true, decimals: 1 },
  patience: { label: "Patience", percent: true, decimals: 1 },
};

function formatPersonaValue(key: string, value: number): string {
  const meta = PERSONA_PARAMETER_META[key] || { label: key, decimals: 2 };
  if (meta.percent) return `${(value * 100).toFixed(meta.decimals ?? 1)}%`;
  if (key === "curve_bias") {
    return `${value < 0 ? "−" : "+"}${Math.abs(value).toFixed(meta.decimals ?? 2)}`;
  }
  const number = value.toFixed(meta.decimals ?? 2);
  return meta.unit ? `${number} ${meta.unit}` : number;
}

function formatPersonaRange(key: string, parameter: PersonaParameter): string {
  if (parameter.signed) {
    return `±${parameter.min.toFixed(2)}–${parameter.max.toFixed(2)}`;
  }
  return `${formatPersonaValue(key, parameter.min)} – ${formatPersonaValue(key, parameter.max)}`;
}

interface Recording {
  id: string;
  path: string;
  label: string;
  source_serial: string;
  steps: number;
}

interface LogEntry {
  id: string;
  text: string;
  ts: string;
}

export default function MightyGridStudio() {
  const [devices, setDevices] = useState<Device[]>([]);
  const [selectedSerials, setSelectedSerials] = useState<string[]>([]);
  const [personas, setPersonas] = useState<Record<string, PersonaMetrics>>({});
  const [activePersona, setActivePersona] = useState<string>("average");
  const [recordings, setRecordings] = useState<Recording[]>([]);
  const [selectedRecording, setSelectedRecording] = useState<string>("");
  const [lockstep, setLockstep] = useState<boolean>(true);
  const [systemState, setSystemState] = useState<"IDLE" | "RECORDING" | "REPLAYING">("IDLE");
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [isScanning, setIsScanning] = useState<boolean>(false);

  const terminalEndRef = useRef<HTMLDivElement>(null);
  const isFetchingRef = useRef<boolean>(false);

  const appendLog = (text: string) => {
    const now = new Date().toLocaleTimeString("en-US", {
      hour12: false,
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
    const uniqueId =
      typeof crypto !== "undefined" && crypto.randomUUID
        ? crypto.randomUUID()
        : `${Date.now()}-${Math.random().toString(36).slice(2, 9)}`;

    setLogs((prev) => [...prev, { id: uniqueId, text, ts: now }]);
  };

  const fetchDashboardData = async () => {
    if (isFetchingRef.current) return;
    isFetchingRef.current = true;
    setIsScanning(true);
    
    // Generate a timestamp to completely bust the browser GET cache
    const cacheBuster = `?t=${Date.now()}`;

    try {
      const [devRes, perRes, recRes] = await Promise.all([
        fetch(`${API_BASE}/api/devices${cacheBuster}`, { cache: "no-store" }).then((r) => r.json()),
        fetch(`${API_BASE}/api/personas${cacheBuster}`, { cache: "no-store" }).then((r) => r.json()),
        fetch(`${API_BASE}/api/recordings${cacheBuster}`, { cache: "no-store" }).then((r) => r.json()),
      ]);

      const fetchedDevs: Device[] = devRes.devices || [];
      setDevices(fetchedDevs);
      setSelectedSerials(fetchedDevs.map((d) => d.serial));
      setPersonas((prev) =>
        Object.keys(prev).length > 0 ? prev : (perRes.personas || {})
      );

      const recs: Recording[] = recRes.recordings || [];
      setRecordings(recs);
      if (recs.length > 0) {
        setSelectedRecording(recs[0].path);
      }
    } catch {
      appendLog("[!] Could not reach FastAPI backend on http://localhost:8000");
    } finally {
      isFetchingRef.current = false;
      setTimeout(() => setIsScanning(false), 350);
    }
  };

  useEffect(() => {
    fetchDashboardData();

    let isMounted = true;
    const ws = new WebSocket(WS_URL);

    ws.onmessage = (event) => {
      if (!isMounted) return;
      try {
        const data = JSON.parse(event.data);
        if (data.message === "__STATUS__:REPLAYING") {
          setSystemState("REPLAYING");
        } else if (data.message === "__STATUS__:IDLE") {
          setSystemState("IDLE");
          fetchDashboardData();
        } else if (data.message) {
          appendLog(data.message);
        }
      } catch {
        appendLog(event.data);
      }
    };

    return () => {
      isMounted = false;
      ws.close();
    };
  }, []);

  useEffect(() => {
    terminalEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [logs]);

  const handleSelectPersona = async (arch: string) => {
    setActivePersona(arch);
    try {
      const res = await fetch(
        `${API_BASE}/api/personas/${encodeURIComponent(arch)}`,
        { cache: "no-store" }
      );
      if (!res.ok) {
        throw new Error(`Persona request failed (${res.status})`);
      }
      const freshPersona: PersonaMetrics = await res.json();
      setPersonas((prev) => ({ ...prev, [arch]: freshPersona }));
    } catch (error) {
      appendLog(`[!] Could not generate a fresh ${arch} persona: ${error}`);
    }
  };

  const toggleDevice = (serial: string) => {
    setSelectedSerials((prev) =>
      prev.includes(serial) ? prev.filter((s) => s !== serial) : [...prev, serial]
    );
  };

  const handleLaunchRecorder = async () => {
    if (selectedSerials.length !== 1) {
      appendLog("[!] Select exactly ONE primary device card to launch the 60FPS Recorder.");
      return;
    }
    let chosenMetrics = personas[activePersona];
    setSystemState("RECORDING");
    try {
      if (chosenMetrics?.seed == null) {
        const personaEndpoint = activePersona === "random"
          ? `${API_BASE}/api/personas/random`
          : `${API_BASE}/api/personas/${encodeURIComponent(activePersona)}`;
        const personaResponse = await fetch(personaEndpoint, { cache: "no-store" });
        if (!personaResponse.ok) {
          throw new Error(`Could not load ${activePersona} persona (${personaResponse.status}).`);
        }
        chosenMetrics = await personaResponse.json() as PersonaMetrics;
        setPersonas((prev) => ({ ...prev, [activePersona]: chosenMetrics! }));
      }
      const seedToUse = chosenMetrics.seed;
      const response = await fetch(`${API_BASE}/api/record/start`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          serial: selectedSerials[0],
          persona: activePersona,
          seed: seedToUse,
        }),
      });
      if (!response.ok) {
        const detail = await response.text();
        throw new Error(`Recorder launch failed (${response.status}): ${detail}`);
      }
      appendLog(
        `[*] Recorder launched with ${activePersona} gesture profile` +
        (seedToUse !== undefined ? ` (seed ${seedToUse}).` : ".")
      );
    } catch (e) {
      appendLog(`[!] Recorder launch failed: ${e}`);
      setSystemState("IDLE");
    }
  };

  const handleLaunchReplay = async () => {
    if (selectedSerials.length === 0) {
      appendLog("[!] Select at least one target device in the fleet.");
      return;
    }
    if (!selectedRecording) {
      appendLog("[!] No recording.json selected.");
      return;
    }
    setLogs([]);
    setSystemState("REPLAYING");

    const chosenMetrics = personas[activePersona];
    const seedToUse = chosenMetrics?.seed;

    try {
      await fetch(`${API_BASE}/api/replay/start`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          recording_path: selectedRecording,
          serials: selectedSerials,
          persona: activePersona,
          seed: seedToUse,
          lockstep,
        }),
      });
    } catch (e) {
      appendLog(`[!] Replay dispatch failed: ${e}`);
      setSystemState("IDLE");
    }
  };

  const handleStopRun = async () => {
    try {
      await fetch(`${API_BASE}/api/run/stop`, { method: "POST" });
      setSystemState("IDLE");
    } catch (e) {
      appendLog(`[!] Stop request failed: ${e}`);
    }
  };

  const currentMetrics: PersonaMetrics = personas[activePersona] || {
    name: "average-0042",
    wpm: 40,
    vigor: 1.05,
    tremor: 1.0,
    scatter: 3.5,
    reaction_ms: 220,
    fling_bias: 0.52,
  };

  const getLogColor = (line: string) => {
    if (line.includes("[+]") || line.includes("PASSED")) return "text-emerald-400";
    if (line.includes("[!]") || line.includes("FAILED") || line.includes("Error") || line.includes("STOP"))
      return "text-rose-400 font-semibold";
    if (line.includes("[*]")) return "text-cyan-300";
    if (line.includes("===")) return "text-violet-400 font-bold tracking-wide";
    return "text-zinc-300";
  };

  return (
    <div className="min-h-screen bg-[#060609] text-zinc-100 font-sans selection:bg-indigo-500/30 relative overflow-hidden flex flex-col">
      <div className="pointer-events-none fixed -top-40 -left-40 w-[520px] h-[520px] rounded-full bg-indigo-600/15 blur-[130px]" />
      <div className="pointer-events-none fixed -bottom-40 -right-40 w-[520px] h-[520px] rounded-full bg-fuchsia-600/10 blur-[140px]" />

      <header className="h-16 border-b border-white/[0.07] bg-[#0B0B10]/80 backdrop-blur-xl px-6 flex items-center justify-between z-10">
        <div className="flex items-center gap-3.5">
          <div className="w-9 h-9 rounded-xl bg-gradient-to-br from-indigo-500 via-violet-500 to-fuchsia-500 flex items-center justify-center shadow-[0_0_25px_-3px_rgba(99,102,241,0.6)] border border-white/20">
            <Zap className="w-5 h-5 text-white fill-white" />
          </div>
          <div>
            <div className="flex items-center gap-2.5">
              <h1 className="text-sm font-bold tracking-wider uppercase text-white">
                MightyGrid Studio
              </h1>
              <span className="text-[10px] font-mono uppercase tracking-widest px-2 py-0.5 rounded-full bg-indigo-500/10 text-indigo-300 border border-indigo-500/20">
                v2.4 Unified
              </span>
            </div>
            <p className="text-xs text-zinc-500">
              Biometric Kinematics &amp; Multi-Device Fleet Orchestration
            </p>
          </div>
        </div>

        <div className="flex items-center gap-3">
          {systemState !== "IDLE" && (
            <button
              onClick={handleStopRun}
              className="inline-flex items-center gap-2 px-3.5 py-1.5 rounded-full text-xs font-bold uppercase tracking-wider bg-rose-500/20 hover:bg-rose-500/30 text-rose-300 border border-rose-500/40 transition-all cursor-pointer active:scale-95"
            >
              <Square className="w-3 h-3 fill-rose-400 text-rose-400" />
              Abort Run
            </button>
          )}
          <div
            className={`flex items-center gap-2.5 px-3.5 py-1.5 rounded-full text-xs font-semibold tracking-wide border transition-all duration-300 ${
              systemState === "IDLE"
                ? "bg-emerald-500/10 text-emerald-300 border-emerald-500/25 shadow-[0_0_20px_-5px_rgba(16,185,129,0.35)]"
                : systemState === "RECORDING"
                ? "bg-rose-500/15 text-rose-300 border-rose-500/30 shadow-[0_0_20px_-5px_rgba(244,63,94,0.5)]"
                : "bg-indigo-500/15 text-indigo-300 border-indigo-500/30 shadow-[0_0_20px_-5px_rgba(99,102,241,0.5)]"
            }`}
          >
            <span
              className={`w-2 h-2 rounded-full ${
                systemState === "IDLE"
                  ? "bg-emerald-400"
                  : systemState === "RECORDING"
                  ? "bg-rose-400 animate-ping"
                  : "bg-indigo-400 animate-pulse"
              }`}
            />
            {systemState === "IDLE" && "SYSTEM READY"}
            {systemState === "RECORDING" && "VIEWPORT RECORDING"}
            {systemState === "REPLAYING" && `LOCKSTEP REPLAY (${currentMetrics.name.toUpperCase()})`}
          </div>
        </div>
      </header>

      <main className="flex-1 grid grid-cols-12 gap-5 p-6 max-w-[1600px] w-full mx-auto z-10">
        <div className="col-span-12 lg:col-span-5 xl:col-span-4 flex flex-col gap-5">
          <section className="rounded-2xl bg-[#0E0E14]/90 border border-white/[0.08] shadow-2xl p-5 flex flex-col gap-4">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <Smartphone className="w-4 h-4 text-indigo-400" />
                <h2 className="text-xs font-bold uppercase tracking-wider text-zinc-300">
                  Connected ADB Fleet
                </h2>
              </div>
              <div className="flex items-center gap-2">
                <span className="text-xs font-mono text-indigo-400 bg-indigo-500/10 border border-indigo-500/20 px-2.5 py-0.5 rounded-md">
                  {selectedSerials.length}/{devices.length} Active
                </span>
                <button
                  onClick={fetchDashboardData}
                  className="inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-xs font-medium text-zinc-300 bg-zinc-900/90 hover:bg-zinc-800 border border-white/10 hover:border-white/20 transition-all duration-150 active:scale-95 cursor-pointer"
                >
                  <RefreshCw className={`w-3.5 h-3.5 ${isScanning ? "animate-spin text-indigo-400" : ""}`} />
                  Scan
                </button>
              </div>
            </div>

            <div className="flex flex-col gap-2.5 max-h-56 overflow-y-auto pr-1">
              {devices.length === 0 ? (
                <div className="py-8 text-center border border-dashed border-white/10 rounded-xl text-zinc-500 text-xs">
                  No Android devices detected over ADB.
                </div>
              ) : (
                devices.map((dev) => {
                  const isSelected = selectedSerials.includes(dev.serial);
                  return (
                    <div
                      key={dev.serial}
                      onClick={() => toggleDevice(dev.serial)}
                      className={`group cursor-pointer rounded-xl p-3.5 border transition-all duration-200 flex items-center justify-between ${
                        isSelected
                          ? "bg-gradient-to-r from-indigo-950/60 via-indigo-900/20 to-transparent border-indigo-500/50 shadow-[0_0_25px_-8px_rgba(99,102,241,0.4)]"
                          : "bg-zinc-900/40 border-white/[0.06] hover:border-white/15 hover:bg-zinc-900/70"
                      }`}
                    >
                      <div className="flex items-center gap-3.5">
                        <div
                          className={`w-5 h-5 rounded-md flex items-center justify-center transition-all ${
                            isSelected
                              ? "bg-indigo-500 text-white shadow-[0_0_12px_rgba(99,102,241,0.7)]"
                              : "bg-zinc-800 border border-white/15 text-transparent"
                          }`}
                        >
                          <Check className="w-3.5 h-3.5 stroke-[3]" />
                        </div>
                        <div>
                          <div className="text-sm font-semibold text-white group-hover:text-indigo-200 transition-colors">
                            {dev.name}
                          </div>
                          <div className="text-[11px] font-mono text-zinc-400 flex items-center gap-2 mt-0.5">
                            <span>{dev.serial}</span>
                            <span className="text-zinc-600">•</span>
                            <span>Android {dev.android_version}</span>
                            <span className="text-zinc-600">•</span>
                            <span className="uppercase">{dev.soc}</span>
                          </div>
                        </div>
                      </div>

                      <span className="text-[10px] font-mono font-bold tracking-wider uppercase px-2 py-0.5 rounded-md bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">
                        {dev.status}
                      </span>
                    </div>
                  );
                })
              )}
            </div>
          </section>

          <section className="rounded-2xl bg-[#0E0E14]/90 border border-white/[0.08] shadow-2xl p-5 flex flex-col gap-4">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <Sparkles className="w-4 h-4 text-violet-400" />
                <h2 className="text-xs font-bold uppercase tracking-wider text-zinc-300">
                  Biometric Persona DNA
                </h2>
              </div>
              <span className="text-[11px] font-mono text-violet-300 bg-violet-500/10 border border-violet-500/20 px-2 py-0.5 rounded">
                {currentMetrics.name}
              </span>
            </div>

            <div className="grid grid-cols-3 gap-1.5 bg-zinc-950/90 p-1.5 rounded-xl border border-white/[0.06]">
              {["average", "speedy", "careful", "shaky", "restless", "careless", "random"].map((arch) => {
                const active = activePersona === arch;
                return (
                  <button
                    key={arch}
                    onClick={() => handleSelectPersona(arch)}
                    className={`py-2 px-3 rounded-lg text-xs font-semibold capitalize transition-all duration-200 cursor-pointer flex items-center justify-center gap-1.5 ${
                      active
                        ? "bg-gradient-to-r from-indigo-600 to-violet-600 text-white shadow-[0_2px_12px_rgba(99,102,241,0.45)] border border-white/20"
                        : "text-zinc-400 hover:text-zinc-200 hover:bg-white/[0.04]"
                    }`}
                  >
                    {arch === "random" && <Dices className="w-3.5 h-3.5" />}
                    {arch}
                  </button>
                );
              })}
            </div>

            <div className="space-y-3 pt-1">
              {[
                {
                  label: "Typing Speed",
                  val: `${currentMetrics.wpm} WPM`,
                  pct: Math.min(100, (currentMetrics.wpm / 65) * 100),
                },
                {
                  label: "Flick Vigor & Velocity",
                  val: `${currentMetrics.vigor}x`,
                  pct: Math.min(100, (currentMetrics.vigor / 1.4) * 100),
                },
                {
                  label: "Micro-Tremor Amplitude",
                  val: `${currentMetrics.tremor} σ`,
                  pct: Math.min(100, (currentMetrics.tremor / 1.8) * 100),
                },
                {
                  label: "Fitts Scatter Radius",
                  val: `±${currentMetrics.scatter} px`,
                  pct: Math.min(100, (currentMetrics.scatter / 7.5) * 100),
                },
              ].map((trait) => (
                <div key={trait.label} className="space-y-1.5">
                  <div className="flex justify-between text-xs">
                    <span className="text-zinc-400">{trait.label}</span>
                    <span className="font-mono text-zinc-200 font-semibold">{trait.val}</span>
                  </div>
                  <div className="w-full h-1.5 bg-zinc-900 rounded-full overflow-hidden border border-white/[0.05]">
                    <div
                      className="h-full bg-gradient-to-r from-indigo-500 via-violet-500 to-fuchsia-500 rounded-full transition-all duration-500"
                      style={{ width: `${trait.pct}%` }}
                    />
                  </div>
                </div>
              ))}
            </div>

            <div className="rounded-xl border border-white/[0.07] bg-zinc-950/60 p-3">
              <div className="flex items-center justify-between mb-2.5">
                <h3 className="text-[11px] font-bold uppercase tracking-wider text-zinc-300">
                  All Parameters &amp; Allowed Ranges
                </h3>
                <span className="text-[10px] text-zinc-500 font-mono">
                  Seed {currentMetrics.seed ?? "—"}
                </span>
              </div>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 max-h-64 overflow-y-auto pr-1">
                {PERSONA_PARAMETER_ORDER.map((key) => {
                  const parameter = currentMetrics.parameters?.[key];
                  if (!parameter) return null;
                  const meta = PERSONA_PARAMETER_META[key];
                  return (
                    <div
                      key={key}
                      className="min-w-0 rounded-lg border border-white/[0.06] bg-white/[0.02] px-2.5 py-2"
                    >
                      <div className="flex items-start justify-between gap-2">
                        <span className="text-[11px] text-zinc-400 leading-4">{meta.label}</span>
                        <span className="text-[11px] text-zinc-100 font-mono font-semibold whitespace-nowrap">
                          {formatPersonaValue(key, parameter.value)}
                        </span>
                      </div>
                      <div className="mt-1 text-[10px] text-zinc-500 font-mono">
                        Range: {formatPersonaRange(key, parameter)}
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          </section>

          <section className="rounded-2xl bg-[#0E0E14]/90 border border-white/[0.08] shadow-2xl p-5 flex flex-col gap-4">
            <div className="flex items-center justify-between">
              <label className="text-xs font-bold uppercase tracking-wider text-zinc-300 flex items-center gap-2">
                <FolderOpen className="w-4 h-4 text-cyan-400" />
                Session Payload
              </label>

              <button
                onClick={() => setLockstep(!lockstep)}
                className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md text-[11px] font-semibold border transition-all cursor-pointer ${
                  lockstep
                    ? "bg-cyan-500/10 text-cyan-300 border-cyan-500/30"
                    : "bg-zinc-900 text-zinc-500 border-white/10"
                }`}
              >
                {lockstep ? <Lock className="w-3 h-3" /> : <Unlock className="w-3 h-3" />}
                Lockstep Sync {lockstep ? "ON" : "OFF"}
              </button>
            </div>

            <select
              value={selectedRecording}
              onChange={(e) => setSelectedRecording(e.target.value)}
              className="w-full bg-zinc-950 border border-white/10 focus:border-indigo-500 rounded-xl px-3.5 py-2.5 text-xs font-mono text-zinc-200 outline-none transition-colors"
            >
              {recordings.length === 0 ? (
                <option value="">No recorded sessions found in /recordings</option>
              ) : (
                recordings.map((r) => (
                  <option key={r.path} value={r.path}>
                    {r.label} — [{r.source_serial}]
                  </option>
                ))
              )}
            </select>

            <div className="grid grid-cols-1 gap-3 pt-1">
              <button
                onClick={handleLaunchRecorder}
                disabled={systemState !== "IDLE"}
                className="group relative overflow-hidden rounded-xl p-[1px] cursor-pointer disabled:opacity-40 disabled:pointer-events-none transition-transform duration-150 hover:-translate-y-0.5 active:translate-y-0"
              >
                <div className="absolute inset-0 bg-gradient-to-r from-rose-500 via-pink-500 to-orange-500 opacity-90 group-hover:opacity-100 transition-opacity" />
                <div className="relative bg-gradient-to-b from-rose-500/90 to-rose-700/95 group-hover:from-rose-500 group-hover:to-rose-600 rounded-[11px] py-3.5 px-5 flex items-center justify-center gap-3 shadow-[0_10px_30px_-5px_rgba(244,63,94,0.55)] border-t border-white/30">
                  <span className="relative flex h-3 w-3">
                    <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-white opacity-75" />
                    <Disc className="relative inline-flex rounded-full h-3 w-3 text-white" />
                  </span>
                  <span className="text-sm font-bold tracking-wide text-white">
                    Launch 60FPS Viewport Recorder
                  </span>
                </div>
              </button>

              {systemState === "IDLE" ? (
                <button
                  onClick={handleLaunchReplay}
                  className="group relative overflow-hidden rounded-xl p-[1px] cursor-pointer transition-transform duration-150 hover:-translate-y-0.5 active:translate-y-0"
                >
                  <div className="absolute inset-0 bg-gradient-to-r from-indigo-500 via-violet-500 to-cyan-400 opacity-90 group-hover:opacity-100 transition-opacity" />
                  <div className="relative bg-gradient-to-b from-indigo-600/95 to-violet-700/95 group-hover:from-indigo-500 group-hover:to-violet-600 rounded-[11px] py-3.5 px-5 flex items-center justify-center gap-3 shadow-[0_10px_30px_-5px_rgba(99,102,241,0.6)] border-t border-white/30">
                    <Play className="w-4 h-4 text-white fill-white transition-transform group-hover:translate-x-0.5" />
                    <span className="text-sm font-bold tracking-wide text-white">
                      Execute Lockstep Fleet Replay ({selectedSerials.length})
                    </span>
                  </div>
                </button>
              ) : (
                <button
                  onClick={handleStopRun}
                  className="group relative overflow-hidden rounded-xl p-[1px] cursor-pointer transition-transform duration-150 hover:-translate-y-0.5 active:translate-y-0"
                >
                  <div className="absolute inset-0 bg-gradient-to-r from-red-500 via-rose-500 to-amber-500 opacity-95 group-hover:opacity-100 transition-opacity" />
                  <div className="relative bg-gradient-to-b from-red-600 to-rose-800 group-hover:from-red-500 group-hover:to-rose-700 rounded-[11px] py-3.5 px-5 flex items-center justify-center gap-3 shadow-[0_10px_30px_-5px_rgba(239,68,68,0.65)] border-t border-white/30">
                    <Square className="w-4 h-4 text-white fill-white animate-pulse" />
                    <span className="text-sm font-bold tracking-wide text-white">
                      Stop Active Run Immediately
                    </span>
                  </div>
                </button>
              )}
            </div>
          </section>
        </div>

        <div className="col-span-12 lg:col-span-7 xl:col-span-8 flex flex-col">
          <div className="flex-1 rounded-2xl bg-[#09090D] border border-white/[0.08] shadow-2xl flex flex-col overflow-hidden">
            <div className="h-12 px-5 bg-[#111118] border-b border-white/[0.07] flex items-center justify-between">
              <div className="flex items-center gap-3">
                <div className="flex items-center gap-2">
                  <span className="w-3 h-3 rounded-full bg-rose-500/80 border border-rose-400/40" />
                  <span className="w-3 h-3 rounded-full bg-amber-500/80 border border-amber-400/40" />
                  <span className="w-3 h-3 rounded-full bg-emerald-500/80 border border-emerald-400/40" />
                </div>
                <div className="h-4 w-[1px] bg-white/10 mx-1" />
                <div className="flex items-center gap-2 text-xs font-mono text-zinc-400">
                  <TerminalIcon className="w-3.5 h-3.5 text-indigo-400" />
                  <span>telemetry-stream — ws://localhost:8000/ws/logs</span>
                </div>
              </div>

              <button
                onClick={() => setLogs([])}
                className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium text-zinc-400 hover:text-white bg-zinc-900 hover:bg-zinc-800 border border-white/10 transition-colors cursor-pointer"
              >
                <Trash2 className="w-3.5 h-3.5" />
                Clear
              </button>
            </div>

            <div className="flex-1 p-5 overflow-y-auto font-mono text-xs leading-relaxed space-y-1.5 max-h-[680px]">
              {logs.length === 0 ? (
                <div className="h-full flex flex-col items-center justify-center text-zinc-600 gap-2 py-24">
                  <Activity className="w-8 h-8 text-zinc-700 animate-pulse" />
                  <p>Awaiting fleet telemetry or step execution...</p>
                </div>
              ) : (
                logs.map((entry) => (
                  <div
                    key={entry.id}
                    className="flex items-start gap-3 hover:bg-white/[0.02] px-2 py-0.5 rounded"
                  >
                    <span className="text-zinc-600 select-none shrink-0">[{entry.ts}]</span>
                    <span className={`break-all whitespace-pre-wrap ${getLogColor(entry.text)}`}>
                      {entry.text}
                    </span>
                  </div>
                ))
              )}
              <div ref={terminalEndRef} />
            </div>
          </div>
        </div>
      </main>
    </div>
  );
}