import argparse
import concurrent.futures
import json
import math
import random
import sys
import time
import threading
import re
import unicodedata
from pathlib import Path
from typing import Dict, Any, Optional, Tuple, List

import cv2
import lxml.etree as ET
import numpy as np
import uiautomator2 as u2

from recorder_engine import MaaTouchDriver


# =====================================================================
# PERSONA / QA MOTION PROFILE INTEGRATION
# =====================================================================

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PERSONA_DIR = PROJECT_ROOT / "android_telemetry" / "telemetry"

if str(PERSONA_DIR) not in sys.path:
    sys.path.insert(0, str(PERSONA_DIR))

try:
    import personas
except ImportError as exc:
    raise ImportError(
        f"Could not import personas.py from {PERSONA_DIR}. "
        "Verify the project folder structure."
    ) from exc


KINEMATIC_LAWS = {
    "speedy": "ease_out_quad",
    "careful": "ease_in_out_sine",
    "average": "smoothstep",
    "shaky": "smoothstep",
    "restless": "ease_in_quad",
    "careless": "careless_blend",
}

TARGET_TOUCH_RATE_HZ = 75

# Printed at replay start so stale/duplicate engine files are easy to identify.
REPLAY_ENGINE_BUILD_ID = "MG-REPLAY-2026-10-29-R6-DUMP-POPUP-SCROLL-RECOVERY"

REPLAY_PROFILES = {
    "baseline": {
        "label": "Recorded timing",
        "timing_scale": 1.0,
    },
    "slow": {
        "label": "Slower QA timing",
        "timing_scale": 1.5,
    },
    "fast": {
        "label": "Faster QA timing",
        "timing_scale": 0.75,
    },
}


def apply_kinematic_law(
    progress: float,
    law: str,
) -> float:
    """Return bounded progress while preserving exact endpoints."""
    t = max(0.0, min(1.0, float(progress)))

    if law == "linear":
        value = t

    elif law == "ease_out_quad":
        value = 1.0 - (1.0 - t) ** 2

    elif law == "ease_in_quad":
        value = t ** 2

    elif law == "ease_in_out_sine":
        value = (1.0 - math.cos(math.pi * t)) / 2.0

    elif law == "smoothstep":
        value = t * t * (3.0 - 2.0 * t)

    elif law == "ease_out_cubic":
        value = 1.0 - (1.0 - t) ** 3

    elif law == "careless_blend":
        # Keep careless swipes brisk without an excessively abrupt launch.
        cubic = 1.0 - (1.0 - t) ** 3
        smooth = t * t * (3.0 - 2.0 * t)
        value = 0.65 * cubic + 0.35 * smooth

    else:
        raise ValueError(
            f"Unsupported kinematics law: {law}"
        )

    if t <= 0.0:
        return 0.0

    if t >= 1.0:
        return 1.0

    return max(0.0, min(1.0, value))


def choose_persona(
    name: Optional[str] = None,
) -> str:
    """Select an archetype interactively or validate a CLI selection."""
    names = list(personas.ARCHETYPE_NAMES)

    if name:
        if name == "random" or name in names:
            return name

        raise ValueError(
            f"Unknown persona {name!r}. "
            f"Available: {', '.join(names)}, random"
        )

    print("\nSelect a QA persona:")

    for index, archetype in enumerate(names, 1):
        law = KINEMATIC_LAWS.get(archetype, "linear")
        print(
            f"  {index}. {archetype} "
            f"(kinematics: {law})"
        )

    print(f"  {len(names) + 1}. random")

    while True:
        raw = input(
            f"Choose persona [1-{len(names) + 1}]: "
        ).strip()

        if raw.isdigit():
            choice = int(raw)

            if 1 <= choice <= len(names):
                return names[choice - 1]

            if choice == len(names) + 1:
                return "random"

        print("Invalid choice. Enter one of the listed numbers.")


def make_persona(
    archetype: str,
    seed: Optional[int] = None,
):
    persona = personas.generate_persona(
        seed=seed,
        archetype=archetype,
    )

    print("\nSelected persona:")
    print(f"  Name: {persona.name}")
    print(f"  Archetype: {persona.archetype}")
    print(f"  Seed: {persona.seed}")
    print(
        "  Kinematics law: "
        f"{KINEMATIC_LAWS.get(persona.archetype, 'linear')}"
    )
    print(
        f"  Swipe path mode: {persona.swipe_shape} "
        "(sampled per gesture)"
    )
    print(f"  Text rate: {persona.wpm:.1f} WPM")
    print(f"  Tremor scale: {persona.tremor_scale:.2f}")
    print(f"  Dwell scale: {persona.dwell_scale:.2f}")
    print(f"  Flick vigor: {persona.flick_vigor:.2f}")
    print(f"  Recovery budget: {persona.retry_budget}")

    return persona


def type_text_with_maatouch(
    driver: MaaTouchDriver,
    u2_device: u2.Device,
    text: str,
    persona,
    timing_scale: float = 1.0,
    step_id: int = 0,
) -> Dict[str, Any]:
    """Type recorded text with a reproducible persona-specific QA schedule.

    WPM determines the base interval. Burstiness introduces bounded timing
    variation, and fatigue_rate gradually lengthens intervals across longer
    text fields. The text content is not modified and no typos are injected.

    This helper remains available for existing callers that explicitly use
    timed character input. The TEXT_INPUT replay branch below uses
    uiautomator2.send_keys for semantic text actions.
    """
    payload = str(text).replace(
        "\r\n",
        "\n",
    ).replace("\r", "\n")

    safe_wpm = max(
        1.0,
        float(getattr(persona, "wpm", 40.0)),
    )
    safe_scale = max(
        0.1,
        float(timing_scale),
    )
    base_interval = (12.0 / safe_wpm) * safe_scale

    burstiness = max(
        1.0,
        float(getattr(persona, "burstiness", 1.0)),
    )
    fatigue_rate = max(
        0.0,
        min(
            1.0,
            float(getattr(persona, "fatigue_rate", 0.0)),
        ),
    )

    variation = min(
        0.30,
        max(
            0.0,
            (burstiness - 1.0) * 0.18,
        ),
    )

    schedule_seed = (
        int(getattr(persona, "seed", 0))
        + int(step_id) * 104729
    )
    rng = random.Random(schedule_seed)

    intervals = []
    denominator = max(1, len(payload) - 1)

    for index, character in enumerate(payload):
        factor = rng.uniform(
            1.0 - variation,
            1.0 + variation,
        )

        progress = index / denominator
        factor *= 1.0 + (
            0.25 * fatigue_rate * progress
        )

        if character == " ":
            factor *= 1.12
        elif character in ",;:":
            factor *= 1.35
        elif character in ".!?":
            factor *= 1.65

        intervals.append(
            max(
                0.015,
                min(1.5, base_interval * factor),
            )
        )

    process = getattr(driver, "proc", None)

    maatouch_live = bool(
        process is not None
        and getattr(process, "poll", lambda: 1)() is None
        and getattr(process, "stdin", None) is not None
    )

    input_method = (
        "maatouch"
        if maatouch_live
        else "uiautomator2_timed_fallback"
    )

    completed_chars = 0
    started_at = time.perf_counter()

    for index, character in enumerate(payload):
        try:
            if character == "\n":
                if maatouch_live:
                    driver.press_key(66)
                else:
                    u2_device.press("enter")

            elif maatouch_live:
                driver.input_text(character)

            else:
                u2_device.send_keys(
                    character,
                    clear=False,
                )

            completed_chars += 1

        except Exception as exc:
            input_method = "uiautomator2_timed_fallback"
            maatouch_live = False

            try:
                if character == "\n":
                    u2_device.press("enter")
                else:
                    u2_device.send_keys(
                        character,
                        clear=False,
                    )

                completed_chars += 1

            except Exception as fallback_exc:
                return {
                    "method": input_method,
                    "configured_wpm": round(safe_wpm, 2),
                    "burstiness": round(burstiness, 3),
                    "fatigue_rate": round(fatigue_rate, 3),
                    "schedule_seed": schedule_seed,
                    "characters_requested": len(payload),
                    "characters_submitted": completed_chars,
                    "elapsed_seconds": round(
                        time.perf_counter() - started_at,
                        3,
                    ),
                    "error": (
                        f"MaaTouch error: {exc}; "
                        f"fallback error: {fallback_exc}"
                    ),
                }

        if index < len(payload) - 1:
            time.sleep(intervals[index])

    elapsed = time.perf_counter() - started_at

    mean_interval = (
        sum(intervals[:-1])
        / max(1, len(intervals) - 1)
        if len(intervals) > 1
        else 0.0
    )

    return {
        "method": input_method,
        "configured_wpm": round(safe_wpm, 2),
        "burstiness": round(burstiness, 3),
        "fatigue_rate": round(fatigue_rate, 3),
        "schedule_seed": schedule_seed,
        "characters_requested": len(payload),
        "characters_submitted": completed_chars,
        "mean_interval_seconds": round(mean_interval, 4),
        "elapsed_seconds": round(elapsed, 3),
    }


# =====================================================================
# 1. OEM POPUP & PERMISSION WATCHER
# =====================================================================

OEM_ALLOW_PATTERNS = [
    "While using the app",
    "Only this time",
    "Allow",
    "ALLOW",
    "Agree",
    "Continue",
    "Got it",
    "Install anyway",
]


# =====================================================================
# 2.A. PHYSICAL KEYBOARD INJECTOR FOR REPLAY
# =====================================================================

class PhysicalKeyboardInjector:
    def __init__(self, u2_dev, maatouch):
        self.u2_dev = u2_dev
        self.maatouch = maatouch
        self._current_kb_layer = "alpha"
        self._keyboard_layer_caches = {}
        self._keyboard_shift_state = None

    def _load_keyboard_nodes(self, force_fresh=False):
        layer = self._current_kb_layer
        if not force_fresh and layer in self._keyboard_layer_caches:
            return self._keyboard_layer_caches[layer]

        xml_dump = self.u2_dev.dump_hierarchy(compressed=False)
        try:
            root = ET.fromstring(xml_dump.encode("utf-8"))
        except Exception:
            root = ET.fromstring(xml_dump)

        screen_w = max(1, int(self.maatouch.max_x))
        screen_h = max(1, int(self.maatouch.max_y))

        parsed = []
        for elem in root.iter("node"):
            attrs = dict(elem.attrib)
            match = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", attrs.get("bounds", ""))
            if not match: continue
            l, t, r, b = map(int, match.groups())
            w, h = r - l, b - t
            if w <= 0 or h <= 0: continue
            cy = (t + b) / 2
            if cy < screen_h * 0.48 or cy > screen_h * 0.985: continue
            if w > screen_w * 0.85 or h > screen_h * 0.16: continue

            raw_text = attrs.get("text", "").strip().lower()
            raw_desc = attrs.get("content-desc", "").strip().lower()

            primary_char = raw_text or raw_desc
            if len(primary_char) == 1 and primary_char.isalpha():
                labels = {primary_char}
            else:
                cleaned_desc = raw_desc.split(",")[0].strip() if "," in raw_desc and len(raw_desc) <= 6 else raw_desc
                labels = {raw_text, raw_desc, cleaned_desc}
                if not raw_text and not raw_desc:
                    labels.add(attrs.get("hint", "").strip().lower())
            labels.discard("")

            parsed.append({
                "bounds": (l, t, r, b),
                "attrs": attrs,
                "labels": labels,
                "resource_id": attrs.get("resource-id", "").lower(),
                "area": w * h,
                "clickable_rank": 0 if attrs.get("clickable", "false").lower() == "true" else 1,
                "screen_w": screen_w,
                "screen_h": screen_h,
            })

        if not parsed:
            raise RuntimeError("No keyboard keys exposed. Ensure keyboard is open.")
        self._keyboard_layer_caches[layer] = parsed
        return parsed

    def _find_keyboard_key(self, key_name: str):
        semantic = str(key_name).lower()
        aliases = {
            "shift": {"shift", "shift key", "caps lock"},
            "backspace": {"backspace", "delete", "erase", "⌫", "←", "backspace/delete"},
            "space": {"space", "spacebar", " ", "english (us)", "english (india)"},
            "enter": {"enter", "search", "go", "done", "next", "send", "return", "↵", "🔍"},
            "sym_switch": {"?123", "123", "!#1", "?12", "sym", "symbols"},
            "alpha_switch": {"abc", "letters", "alphabet"},
            "more_sym_switch": {"=\\<", "~\\<", "1/2", "2/2", "#+=", "{&="},
        }
        punctuation_names = {
            ",": {",", "comma"}, ".": {".", "dot", "period"},
            "/": {"/", "slash"}, "\\": {"\\", "backslash"},
            "'": {"'", "apostrophe"}, '"': {'"', "double quote"},
            "?": {"?", "question mark"}, "!": {"!", "exclamation mark"},
            "-": {"-", "hyphen"}, "=": {"=", "equals"},
            ":": {":", "colon"}, ";": {";", "semicolon"},
            "@": {"@", "at"}, "#": {"#", "hash"},
            "$": {"$", "dollar"}, "₹": {"₹", "rupee"},
            "%": {"%", "percent"}, "&": {"&", "ampersand"},
            "*": {"*", "asterisk"}, "(": {"(", "left parenthesis"},
            ")": {")", "right parenthesis"}, "_": {"_", "underscore"}, "+": {"+", "plus"},
        }
        if semantic in aliases: accepted = aliases[semantic]
        elif len(key_name) == 1:
            accepted = {semantic}
            accepted.update(punctuation_names.get(key_name, set()))
        else: accepted = {semantic}

        nodes = self._load_keyboard_nodes()
        matches = []
        for node in nodes:
            found = bool(node["labels"].intersection(accepted))
            rid = node["resource_id"]
            if not found:
                if semantic == "space" and "space" in rid: found = True
                elif semantic == "backspace" and any(t in rid for t in ("delete", "backspace")): found = True
                elif semantic == "shift" and "shift" in rid: found = True
                elif semantic in ("sym_switch", "alpha_switch") and any(t in rid for t in ("key_pos_switch", "symbol", "mode_change")): found = True
                elif semantic == "enter" and any(t in rid for t in ("enter", "action")): found = True
                elif len(semantic) == 1 and semantic.isalnum() and f"key_pos_{semantic}" in rid: found = True
            if not found: continue

            l, t, r, b = node["bounds"]
            w, h = r - l, b - t
            if w > node["screen_w"] * (0.80 if semantic == "space" else 0.28) or h > 0.16 * node["screen_h"]: continue
            matches.append(node)

        if not matches:
            raise RuntimeError(f"Key '{key_name}' not found on layer '{self._current_kb_layer}'.")
        
        matches.sort(key=lambda n: (n["area"], n["clickable_rank"]))
        return matches[0]["bounds"], matches[0]["attrs"]

    def _tap_android_keyboard_key(self, key_name: str):
        try:
            bounds, attrs = self._find_keyboard_key(key_name)
            l, t, r, b = bounds
            x, y = int((l + r) / 2), int((t + b) / 2)
        except RuntimeError:
            x, y = self._fallback_key_point(key_name)
            attrs = {}

        pressure = max(1, min(int(self.maatouch.max_pressure), 75))
        # BUMP: 45ms tap duration bypasses OEM palm-rejection filters on system keys
        self.maatouch._send(f"d 0 {x} {y} {pressure}\nc\nw 45\nu 0\nc\n")
        time.sleep(0.050)
        return attrs

    def _switch_keyboard_layer(self, target_layer: str):
        if self._current_kb_layer == target_layer: 
            return
            
        if target_layer == "alpha":
            self._tap_android_keyboard_key("alpha_switch")
            time.sleep(0.25)  # Wait for keyboard morph animation to settle
            self._current_kb_layer = "alpha"
            
        elif target_layer == "sym1":
            if self._current_kb_layer == "alpha": 
                self._tap_android_keyboard_key("sym_switch")
                time.sleep(0.25)
            elif self._current_kb_layer == "sym2": 
                self._tap_android_keyboard_key("sym_switch")
                time.sleep(0.25)
            self._current_kb_layer = "sym1"
            
        elif target_layer == "sym2":
            if self._current_kb_layer == "alpha":
                self._tap_android_keyboard_key("sym_switch")
                time.sleep(0.25)
            self._tap_android_keyboard_key("more_sym_switch")
            time.sleep(0.25)
            self._current_kb_layer = "sym2"

        if target_layer not in self._keyboard_layer_caches:
            time.sleep(0.14)
            self._load_keyboard_nodes(force_fresh=True)

    def _keyboard_shift_is_active(self):
        if self._keyboard_shift_state is not None: return self._keyboard_shift_state
        try: _, attrs = self._find_keyboard_key("shift")
        except Exception: return None
        vals = [attrs.get(n, "").strip().lower() for n in ("selected", "checked", "activated")]
        if any(v in ("true", "1") for v in vals): self._keyboard_shift_state = True
        elif any(v in ("false", "0") for v in vals): self._keyboard_shift_state = False
        else: return None
        return self._keyboard_shift_state

    def _ensure_keyboard_shift(self, desired: bool):
        active = self._keyboard_shift_is_active()
        if active is None:
            if desired: self._tap_android_keyboard_key("shift")
            self._keyboard_shift_state = desired
            return
        if active != desired: self._tap_android_keyboard_key("shift")
        self._keyboard_shift_state = desired

    def _tap_keyboard_character(self, character: str):
        if character == " ":
            self._tap_android_keyboard_key("space")
            if self._current_kb_layer != "alpha": self._current_kb_layer = "alpha"
            return
        if character in "\r\n":
            self._tap_android_keyboard_key("enter")
            return

        if character.isalpha():
            if self._current_kb_layer != "alpha": self._switch_keyboard_layer("alpha")
            self._ensure_keyboard_shift(character.isupper())
            self._tap_android_keyboard_key(character.lower())
            self._keyboard_shift_state = False
            return

        if self._current_kb_layer == "alpha" and character in (".", ","):
            try:
                self._tap_android_keyboard_key(character)
                return
            except Exception: pass

        if self._current_kb_layer == "alpha": self._switch_keyboard_layer("sym1")
        try:
            self._tap_android_keyboard_key(character)
            return
        except Exception: pass

        try:
            self._switch_keyboard_layer("sym2")
            self._tap_android_keyboard_key(character)
            return
        except Exception:
            self._load_keyboard_nodes(force_fresh=True)
            self._tap_android_keyboard_key(character)

    def _fallback_key_point(self, key_name: str) -> tuple[int, int]:
        """Geometric fallback for completely obfuscated OEM keyboards."""
        semantic = str(key_name).strip().lower()
        width, height = max(1, int(self.maatouch.max_x)), max(1, int(self.maatouch.max_y))
        layer = self._current_kb_layer

        # REVERTED Y-COORDINATES: Restored to original values for perfect row alignment
        if semantic in {"space", "spacebar", "space bar", " "}: x, y = 0.535, 0.910
        elif semantic in {"enter", "return", "search", "done", "↵"}: x, y = 0.935, 0.910
        elif semantic in {"backspace", "delete", "erase", "⌫"}: x, y = 0.925, 0.840
        elif semantic in {"shift", "caps lock"}: x, y = 0.080, 0.840
        elif semantic in {"sym_switch", "alpha_switch", "?123", "abc"}: x, y = 0.110, 0.900
        elif semantic in {"more_sym_switch", "=\\<"}: x, y = 0.110, 0.840
        elif layer == "sym2" and semantic == "<": x, y = 0.170, 0.910
        elif layer == "sym2" and semantic == ">": x, y = 0.820, 0.910
        elif semantic in {",", "comma"}: x, y = 0.215, 0.910
        elif semantic in {".", "dot", "period"}: x, y = 0.820, 0.910
        elif layer == "alpha" and len(key_name) == 1 and semantic in "qwertyuiop": x, y = ("qwertyuiop".index(semantic) + 0.5) / 10.0, 0.720
        elif layer == "alpha" and len(key_name) == 1 and semantic in "asdfghjkl": x, y = ("asdfghjkl".index(semantic) + 1.0) / 10.0, 0.780
        elif layer == "alpha" and len(key_name) == 1 and semantic in "zxcvbnm": x, y = 0.18 + "zxcvbnm".index(semantic) * 0.092, 0.840
        elif layer == "sym2" and len(key_name) == 1 and semantic in "~`|•√π÷×§δ": x, y = ("~`|•√π÷×§δ".index(semantic) + 0.5) / 10.0, 0.720
        elif layer == "sym2" and len(key_name) == 1 and semantic in "€¥$¢^°−{}\\": x, y = ("€¥$¢^°−{}\\".index(semantic) + 0.5) / 10.0, 0.780
        elif layer == "sym2" and len(key_name) == 1 and semantic in "%©®™✓[]": x, y = {"%": 0.05, "©": 0.17, "®": 0.29, "™": 0.41, "✓": 0.55, "[": 0.67, "]": 0.78}[semantic], 0.840
        elif layer == "sym1" and len(key_name) == 1 and semantic in "1234567890": x, y = ("1234567890".index(semantic) + 0.5) / 10.0, 0.720
        elif layer == "sym1" and len(key_name) == 1 and semantic in "@#₹_&-+()/": x, y = ("@#₹_&-+()/".index(semantic) + 0.5) / 10.0, 0.780
        elif layer == "sym1" and len(key_name) == 1 and semantic in "*\"':;!?": x, y = [0.22, 0.34, 0.43, 0.53, 0.63, 0.73, 0.82]["*\"':;!?".index(semantic)], 0.840
        elif layer == "sym1" and len(key_name) == 1 and semantic == "_": x, y = 0.35, 0.780
        elif layer == "sym1" and len(key_name) == 1 and semantic == "-": x, y = 0.55, 0.780
        elif layer == "sym1" and len(key_name) == 1 and semantic == "+": x, y = 0.65, 0.780
        else: raise RuntimeError(f"No geometric fallback for '{key_name}' on layer '{layer}'.")

        return int(round(x * width)), int(round(y * height))
    
# =====================================================================
# 2. SINGLE-DEVICE REPLAY WORKER
# =====================================================================

class DeviceReplayWorker:
    def __init__(
        self,
        serial: str,
        recording_path: Path,
        parsed_data: Dict[str, Any],
        start_barrier: Optional[threading.Barrier] = None,
        lockstep_barriers: Optional[List[threading.Barrier]] = None,
        persona=None,
        profile_name: str = "baseline",
    ):
        self.serial = serial
        self.recording_dir = recording_path.parent
        self.data = parsed_data
        self.start_barrier = start_barrier
        self.lockstep_barriers = lockstep_barriers

        self.persona = persona or personas.generate_persona(
            archetype="average"
        )

        self.kinematic_law = KINEMATIC_LAWS.get(
            self.persona.archetype,
            "linear",
        )

        if profile_name not in REPLAY_PROFILES:
            raise ValueError(
                f"Unknown timing profile: {profile_name}"
            )

        self.profile_name = profile_name
        self.timing_scale = REPLAY_PROFILES[
            profile_name
        ]["timing_scale"]

        self.u2_dev: Optional[u2.Device] = None
        self.maatouch: Optional[MaaTouchDriver] = None
        self._gesture_counter = 0
        # Last live hierarchy captured by the resolver; saved on failure so each
        # mismatch is diagnosable from XML/screenshot instead of guessed at.
        self._last_live_snapshot: Dict[str, Any] = {}
        self._diagnostics_dir = (
            self.recording_dir / "replay_diagnostics"
            / re.sub(r"[^A-Za-z0-9_.-]+", "_", str(self.serial))
        )

    def execute_test(self) -> Dict[str, Any]:
        print(
            f"[*] Replay worker source: {Path(__file__).resolve()} | "
            f"build={REPLAY_ENGINE_BUILD_ID}",
            flush=True,
        )
        self.u2_dev = u2.connect(self.serial)

        self.maatouch = MaaTouchDriver(self.serial)
        self.maatouch.start()

        self._prepare_initial_state()

        if self.start_barrier is not None:
            try:
                self.start_barrier.wait(timeout=15.0)
            except threading.BrokenBarrierError:
                print(
                    f"[!] Start barrier timeout on {self.serial}, "
                    "proceeding..."
                )

        results = []
        steps = self.data["steps"]
        last_swipe_trajectory = None

        try:
            print(
                f"[*] Replay loop started: {len(steps)} steps on {self.serial}.",
                flush=True,
            )
            for index, step in enumerate(steps):
                if (
                    self.lockstep_barriers
                    and index < len(self.lockstep_barriers)
                ):
                    try:
                        self.lockstep_barriers[index].wait(
                            timeout=12.0
                        )
                    except threading.BrokenBarrierError:
                        print(
                            f"[!] Step {index + 1}: lockstep barrier timed out; continuing.",
                            flush=True,
                        )

                event_type = step["event_type"]
                print(
                    f"[*] Step {index + 1}/{len(steps)} "
                    f"(id={step.get('step_id', '?')}, type={event_type}) started.",
                    flush=True,
                )

                # Replay every recorded swipe. Do not infer that it is redundant
                # from the visibility of a later tap target: it may be required
                # for timing, navigation state, or the original gesture sequence.
                next_step = steps[index + 1] if index + 1 < len(steps) else None
                step_result = self._run_step(
                    step,
                    last_swipe_trajectory,
                    next_step=next_step,
                )
                results.append(step_result)
                step_reason = str(step_result.get("reason", "") or "")
                print(
                    f"[{'+' if step_result.get('status') == 'PASSED' else '!'}] "
                    f"Step {index + 1}/{len(steps)} "
                    f"status={step_result.get('status', 'UNKNOWN')}"
                    + (f" — {step_reason}" if step_reason else ""),
                    flush=True,
                )

                if (
                    event_type == "SWIPE"
                    and step.get("trajectory")
                ):
                    last_swipe_trajectory = step["trajectory"]

                elif event_type != "SWIPE":
                    last_swipe_trajectory = None

                if step_result["status"] == "FAILED":
                    break

            # NEW: Let the final touch_up command fully flush to Android before severing the pipe
            time.sleep(0.5)

        finally:
            try:
                self.maatouch.stop()
            except Exception as stop_exc:
                print(
                    f"[!] MaaTouch cleanup warning on {self.serial}: {stop_exc}",
                    flush=True,
                )

        passed_count = sum(1 for item in results if item.get("status") == "PASSED")
        skipped_count = sum(
            1 for item in results
            if str(item.get("status", "")).startswith("SKIPPED")
        )
        failed_count = sum(1 for item in results if item.get("status") == "FAILED")
        not_run_count = max(0, len(steps) - len(results))
        print(
            f"[*] Replay finished on {self.serial}: "
            f"{passed_count}/{len(steps)} passed; {skipped_count} skipped; "
            f"{failed_count} failed; {not_run_count} not run.",
            flush=True,
        )

        return {
            "serial": self.serial,
            "persona": self.persona.name,
            "persona_archetype": self.persona.archetype,
            "persona_seed": self.persona.seed,
            "kinematics_law": self.kinematic_law,
            "swipe_path_family": self.persona.swipe_shape,
            "text_input_wpm": round(self.persona.wpm, 2),
            "tremor_scale": round(self.persona.tremor_scale, 3),
            "dwell_scale": round(self.persona.dwell_scale, 3),
            "flick_vigor": round(self.persona.flick_vigor, 3),
            "retry_budget": self.persona.retry_budget,
            "timing_profile": self.profile_name,
            "timing_scale": self.timing_scale,
            "completed_steps": sum(
                1 for item in results if item.get("status") == "PASSED"
            ),
            "skipped_steps": sum(
                1 for item in results
                if str(item.get("status", "")).startswith("SKIPPED")
            ),
            "attempted_steps": len(results),
            "failed_steps": sum(
                1 for item in results if item.get("status") == "FAILED"
            ),
            "stopped_at_step": (
                results[-1].get("step_id")
                if results and results[-1].get("status") == "FAILED"
                else None
            ),
            "remaining_steps_not_run": max(0, len(steps) - len(results)),
            "total_steps": len(steps),
            "steps": results,
        }

    def _prepare_initial_state(self):
        """Wake the device and prepare its recorded starting state."""
        print(
            f"[*] Resetting initial state for {self.serial}..."
        )

        try:
            self.u2_dev.screen_on()

            steps = self.data.get("steps", [])
            if not steps:
                return

            first_package = steps[0].get("pre_package", "")

            launcher_keywords = [
                "launcher",
                "miui.home",
                "sec.android.app",
                "nexuslauncher",
            ]

            is_home = (
                not first_package
                or any(
                    keyword in first_package.lower()
                    for keyword in launcher_keywords
                )
            )

            if is_home:
                self.u2_dev.press("home")
                time.sleep(0.5)
                self.u2_dev.press("home")
                time.sleep(1.0)

            else:
                self.u2_dev.app_start(
                    first_package,
                    stop=False,
                )
                time.sleep(2.0)

        except Exception as exc:
            print(
                f"[!] Pre-flight state warning on "
                f"{self.serial}: {exc}"
            )

    def _peek_next_tap_step(
        self,
        steps: List[Dict[str, Any]],
        start_idx: int,
    ) -> Optional[Dict[str, Any]]:
        """Find the first tap/long-press after a swipe chain."""
        for step in steps[start_idx + 1:]:
            if step["event_type"] == "SWIPE":
                continue

            if step["event_type"] in ("TAP", "LONG_PRESS"):
                return step

            break

        return None

    def _is_anchor_currently_visible(
        self,
        anchor: Dict[str, Any],
    ) -> bool:
        if not anchor:
            return False

        if not (
            anchor.get("text")
            or anchor.get("content_desc")
            or anchor.get("resource_id")
        ):
            return False

        bounds = self._find_bounds_in_hierarchy(anchor, (0.5, 0.5))
        # The negative bounds tuple means a system dialog was handled, not
        # that the requested target is visible.
        return bool(bounds) and bounds != (-1, -1, -1, -1)

    def _run_step(
        self,
        step: Dict[str, Any],
        prev_swipe_trajectory: Optional[List[Dict[str, Any]]] = None,
        next_step: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        step_id = step["step_id"]
        event_type = step["event_type"]

        think_seconds = min(
            (
                step.get("think_time_ms", 300) / 1000.0
            ) * self.timing_scale,
            2.0,
        ) + max(
            0.0,
            self.persona.reaction_ms / 1000.0,
        )

        if think_seconds > 0:
            time.sleep(think_seconds)

        if event_type == "KEY_EVENT":
            package_before, activity_before, xml_before = self._capture_live_ui_snapshot()
            snapshot_before_action = {
                "package": package_before, "activity": activity_before,
                "xml": xml_before, "captured_at": time.time(),
            }
            self._last_live_snapshot = dict(snapshot_before_action)
            precondition_ok, precondition_reason = self._validate_step_precondition(
                step, package_before, activity_before, xml_before
            )
            if not precondition_ok:
                diagnostic_path = self._save_step_diagnostic(
                    step_id, "key_event_precondition_failed", step.get("anchor") or {},
                    precondition_reason,
                )
                if diagnostic_path:
                    precondition_reason += f" | live dump: {diagnostic_path}"
                return {
                    "step_id": step_id,
                    "type": event_type,
                    "status": "FAILED",
                    "reason": precondition_reason,
                }
            try:
                self.maatouch.press_key(int(step["keycode"]))
            except Exception as exc:
                return {
                    "step_id": step_id,
                    "type": event_type,
                    "status": "FAILED",
                    "reason": f"KEY_EVENT dispatch failed: {exc}",
                }
            transition_ok, transition_reason = self._verify_expected_next_package(
                step, next_step, package_before
            )
            if transition_ok:
                transition_ok, target_reason = self._verify_expected_next_target(
                    next_step, package_before, snapshot_before_action
                )
                if not transition_reason or transition_reason.startswith("dispatch_confirmed"):
                    transition_reason = target_reason
            if not transition_ok:
                diagnostic_path = self._save_step_diagnostic(
                    step_id, "post_action_state_failed", step.get("anchor") or {},
                    transition_reason,
                )
                if diagnostic_path:
                    transition_reason += f" | live dump: {diagnostic_path}"
                return {
                    "step_id": step_id,
                    "type": event_type,
                    "status": "FAILED",
                    "reason": transition_reason,
                }
            return {
                "step_id": step_id,
                "type": event_type,
                "status": "PASSED",
                "postcondition": transition_reason or "dispatch_confirmed",
            }

        if event_type == "TEXT_INPUT":
            package_before, activity_before, xml_before = self._capture_live_ui_snapshot()
            snapshot_before_action = {
                "package": package_before, "activity": activity_before,
                "xml": xml_before, "captured_at": time.time(),
            }
            self._last_live_snapshot = dict(snapshot_before_action)
            precondition_ok, precondition_reason = self._validate_step_precondition(
                step, package_before, activity_before, xml_before
            )
            if precondition_ok:
                precondition_ok, precondition_reason = self._focused_input_is_ready(
                    xml_before, step.get("anchor") or {}
                )
            if not precondition_ok:
                diagnostic_path = self._save_step_diagnostic(
                    step_id, "text_input_precondition_failed", step.get("anchor") or {},
                    precondition_reason,
                )
                if diagnostic_path:
                    precondition_reason += f" | live dump: {diagnostic_path}"
                return {
                    "step_id": step_id,
                    "type": event_type,
                    "status": "FAILED",
                    "reason": precondition_reason,
                }
            payload = step.get("text_payload", "")

            if not isinstance(payload, str) or not payload:
                return {
                    "step_id": step_id,
                    "type": event_type,
                    "status": "FAILED",
                    "reason": "TEXT_INPUT has no valid text_payload.",
                }

            try:
                # 1. Initialize the physical keyboard injector once for this device run
                if not hasattr(self, "kb_injector"):
                    self.kb_injector = PhysicalKeyboardInjector(self.u2_dev, self.maatouch)

                # 2. Calculate Persona Timing Dynamics
                safe_wpm = max(1.0, float(getattr(self.persona, "wpm", 40.0)))
                base_interval = (12.0 / safe_wpm) * self.timing_scale
                burstiness = max(1.0, float(getattr(self.persona, "burstiness", 1.0)))
                variation = min(0.30, max(0.0, (burstiness - 1.0) * 0.18))
                
                schedule_seed = int(getattr(self.persona, "seed", 0)) + int(step_id) * 104729
                rng = random.Random(schedule_seed)

                completed_chars = 0
                started_at = time.perf_counter()

                # 3. Physically tap the screen for each character
                for index, character in enumerate(payload):
                    self.kb_injector._tap_keyboard_character(character)
                    completed_chars += 1
                    
                    if index < len(payload) - 1:
                        factor = rng.uniform(1.0 - variation, 1.0 + variation)
                        if character == " ": factor *= 1.12
                        elif character in ",;:": factor *= 1.35
                        elif character in ".!?": factor *= 1.65
                        
                        # Subtract the ~25ms physical tap duration from the sleep
                        sleep_time = max(0.015, base_interval * factor)
                        adjusted_sleep = max(0.001, sleep_time - 0.025)
                        time.sleep(adjusted_sleep)

                elapsed = time.perf_counter() - started_at
                text_verified, text_reason = self._verify_text_payload_observed(
                    payload, step.get("anchor") or {}
                )
                if text_verified is False:
                    diagnostic_path = self._save_step_diagnostic(
                        step_id, "text_input_content_mismatch", step.get("anchor") or {},
                        text_reason,
                    )
                    if diagnostic_path:
                        text_reason += f" | live dump: {diagnostic_path}"
                    return {
                        "step_id": step_id,
                        "type": event_type,
                        "status": "FAILED",
                        "reason": text_reason,
                    }
                next_ready, next_reason = self._verify_expected_next_target(
                    next_step, package_before, snapshot_before_action
                )
                if not next_ready:
                    diagnostic_path = self._save_step_diagnostic(
                        step_id, "text_input_next_target_not_ready", {}, next_reason
                    )
                    if diagnostic_path:
                        next_reason += f" | live dump: {diagnostic_path}"
                    return {
                        "step_id": step_id,
                        "type": event_type,
                        "status": "FAILED",
                        "reason": next_reason,
                    }

                return {
                    "step_id": step_id,
                    "type": event_type,
                    "status": "PASSED",
                    "postcondition": f"{text_reason}; {next_reason}",
                    "text_input": {
                        "method": "maatouch_physical_typing",
                        "characters": completed_chars,
                        "elapsed_seconds": round(elapsed, 3),
                        "wpm_achieved": round((completed_chars / 5.0) / (elapsed / 60.0), 1) if elapsed > 0 else 0
                    },
                }

            except Exception as exc:
                return {
                    "step_id": step_id,
                    "type": event_type,
                    "status": "FAILED",
                    "reason": f"Physical text injection failed: {exc}",
                }

        trajectory = step.get("trajectory") or []

        if not trajectory:
            return {
                "step_id": step_id,
                "type": event_type,
                "status": "SKIPPED",
            }

        if event_type == "SWIPE":
            package_before, activity_before, xml_before = self._capture_live_ui_snapshot()
            snapshot_before_action = {
                "package": package_before, "activity": activity_before,
                "xml": xml_before, "captured_at": time.time(),
            }
            self._last_live_snapshot = dict(snapshot_before_action)
            precondition_ok, precondition_reason = self._validate_step_precondition(
                step, package_before, activity_before, xml_before
            )
            if not precondition_ok:
                diagnostic_path = self._save_step_diagnostic(
                    step_id, "swipe_precondition_failed", step.get("anchor") or {},
                    precondition_reason,
                )
                if diagnostic_path:
                    precondition_reason += f" | live dump: {diagnostic_path}"
                return {
                    "step_id": step_id,
                    "type": "SWIPE",
                    "status": "FAILED",
                    "reason": precondition_reason,
                }
            width = self.maatouch.max_x
            height = self.maatouch.max_y

            first = trajectory[0]
            start_x = int(first["rx"] * width)
            start_y = int(first["ry"] * height)

            try:
                self._inject_scaled_trajectory(
                    start_x,
                    start_y,
                    trajectory,
                    gesture_type=event_type,
                )
            except Exception as exc:
                reason = f"MaaTouch swipe dispatch failed: {exc}"
                diagnostic_path = self._save_step_diagnostic(
                    step_id, "swipe_dispatch_failed", {}, reason
                )
                if diagnostic_path:
                    reason += f" | live dump: {diagnostic_path}"
                return {
                    "step_id": step_id,
                    "type": "SWIPE",
                    "status": "FAILED",
                    "reason": reason,
                }

            time.sleep(0.55)
            next_ready, next_reason = self._verify_expected_next_target(
                next_step, package_before, snapshot_before_action
            )
            if not next_ready:
                diagnostic_path = self._save_step_diagnostic(
                    step_id, "swipe_next_target_not_ready", {}, next_reason
                )
                if diagnostic_path:
                    next_reason += f" | live dump: {diagnostic_path}"
                return {
                    "step_id": step_id,
                    "type": "SWIPE",
                    "status": "FAILED",
                    "reason": next_reason,
                    "match_tier": "SCREEN_NORMALIZED_SWIPE",
                    "injected_start": (start_x, start_y),
                }

            return {
                "step_id": step_id,
                "type": "SWIPE",
                "status": "PASSED",
                "match_tier": "SCREEN_NORMALIZED_SWIPE",
                "injected_start": (start_x, start_y),
                "postcondition": next_reason,
            }

        anchor = step.get("anchor") or {}

        expected_package = str(
            step.get("pre_package") or anchor.get("package") or ""
        ).strip()

        resolved_xy, match_tier = self._wait_and_resolve_target(
            anchor,
            trajectory[0],
            timeout_s=8.0,
            scroll_recovery_trajectory=prev_swipe_trajectory,
            expected_package=expected_package,
            step_id=step_id,
        )

        if resolved_xy is None:
            reason = (
                f"Could not resolve target ({match_tier}). "
                f"expected_package='{expected_package}', "
                f"text='{anchor.get('text')}', "
                f"desc='{anchor.get('content_desc')}', "
                f"id='{anchor.get('resource_id')}', "
                f"visual_crop='{anchor.get('visual_crop_path')}'"
            )
            diagnostic_path = self._save_step_diagnostic(
                step_id, "target_not_resolved", anchor, reason
            )
            if diagnostic_path:
                reason += f" | live dump: {diagnostic_path}"
            return {
                "step_id": step_id,
                "type": event_type,
                "status": "FAILED",
                "reason": reason,
            }

        start_x, start_y = resolved_xy

        snapshot_before_action = dict(self._last_live_snapshot or {})
        package_before_action = str(snapshot_before_action.get("package") or self._current_package())
        try:
            self._inject_scaled_trajectory(
                start_x,
                start_y,
                trajectory,
                gesture_type=event_type,
            )
        except Exception as exc:
            diagnostic_path = self._save_step_diagnostic(
                step_id, "input_dispatch_failed", anchor,
                f"MaaTouch trajectory dispatch failed: {exc}",
            )
            reason = f"MaaTouch trajectory dispatch failed: {exc}"
            if diagnostic_path:
                reason += f" | live dump: {diagnostic_path}"
            return {
                "step_id": step_id,
                "type": event_type,
                "status": "FAILED",
                "reason": reason,
            }

        transition_ok, transition_reason = self._verify_expected_next_package(
            step, next_step, package_before_action
        )
        if transition_ok:
            transition_ok, target_reason = self._verify_expected_next_target(
                next_step, package_before_action, snapshot_before_action
            )
            if not transition_reason or transition_reason.startswith("dispatch_confirmed"):
                transition_reason = target_reason
        if not transition_ok:
            diagnostic_path = self._save_step_diagnostic(
                step_id, "post_action_state_failed", anchor,
                transition_reason,
            )
            reason = transition_reason
            if diagnostic_path:
                reason += f" | live dump: {diagnostic_path}"
            return {
                "step_id": step_id,
                "type": event_type,
                "status": "FAILED",
                "reason": reason,
                "match_tier": match_tier,
                "injected_start": (start_x, start_y),
            }

        return {
            "step_id": step_id,
            "type": event_type,
            "status": "PASSED",
            "match_tier": match_tier,
            "injected_start": (start_x, start_y),
            "postcondition": transition_reason or "dispatch_confirmed",
        }

    # -----------------------------------------------------------------
    # 3-TIER TARGET RESOLUTION + AUTO-SCROLL RECOVERY
    # -----------------------------------------------------------------

    def _uia_dimensions(self) -> Tuple[int, int]:
        """Get dimensions in the same coordinate space as UIAutomator bounds."""
        try:
            width, height = self.u2_dev.window_size()
            width, height = int(width), int(height)
            if width > 0 and height > 0:
                return width, height
        except Exception:
            pass
        return max(1, int(self.maatouch.max_x)), max(1, int(self.maatouch.max_y))

    def _uia_to_touch_point(self, x: int, y: int) -> Tuple[int, int]:
        """Convert UIAutomator coordinates into the active MaaTouch range."""
        ui_w, ui_h = self._uia_dimensions()
        touch_w = max(1, int(self.maatouch.max_x))
        touch_h = max(1, int(self.maatouch.max_y))
        tx = round(int(x) * touch_w / ui_w)
        ty = round(int(y) * touch_h / ui_h)
        return (
            max(0, min(touch_w - 1, tx)),
            max(0, min(touch_h - 1, ty)),
        )

    @staticmethod
    def _normalise_ui_label(value: Any) -> str:
        """Normalize UI labels across whitespace, Unicode, and punctuation."""
        value = unicodedata.normalize("NFKC", str(value or "")).casefold()
        value = re.sub(r"[\u200b-\u200f\ufeff]", "", value)
        value = re.sub(r"[^\w]+", " ", value, flags=re.UNICODE)
        return " ".join(value.split())

    def _current_package(self) -> str:
        try:
            current = self.u2_dev.app_current() or {}
            return str(current.get("package", "") or "").strip()
        except Exception:
            return ""

    def _capture_live_ui_snapshot(self) -> Tuple[str, str, Optional[str]]:
        """Capture a foreground package and hierarchy from the same stable UI state.

        Two foreground reads bracket the hierarchy dump. If the package changes
        mid-dump, retry rather than assigning XML from screen A to package B.
        """
        device = self.u2_dev
        if device is None:
            return "", "", None
        last_package = ""
        last_activity = ""
        last_xml: Optional[str] = None
        for _ in range(3):
            try:
                before = device.app_current() or {}
                before_package = str(before.get("package", "") or "").strip()
                before_activity = str(before.get("activity", "") or "").strip()
                xml_dump = device.dump_hierarchy(compressed=False)
                after = device.app_current() or {}
                after_package = str(after.get("package", "") or "").strip()
                after_activity = str(after.get("activity", "") or "").strip()
                last_package = after_package
                last_activity = after_activity
                last_xml = xml_dump
                same_package = before_package == after_package
                same_activity = not (before_activity and after_activity) or before_activity == after_activity
                if same_package and same_activity and xml_dump:
                    return after_package, last_activity, xml_dump
            except Exception:
                time.sleep(0.08)
        # Never return a possibly mismatched hierarchy as trusted evidence.
        return last_package, last_activity, None

    def _save_step_diagnostic(
        self,
        step_id: int,
        stage: str,
        anchor: Optional[Dict[str, Any]],
        reason: str,
    ) -> Optional[str]:
        """Save the live XML/screenshot on failure for deterministic debugging."""
        device = self.u2_dev
        try:
            self._diagnostics_dir.mkdir(parents=True, exist_ok=True)
            base = self._diagnostics_dir / f"step_{int(step_id):03d}_{stage}"
            snapshot = getattr(self, "_last_live_snapshot", {}) or {}
            package = str(snapshot.get("package", "") or "")
            activity = str(snapshot.get("activity", "") or "")
            xml_dump = snapshot.get("xml")
            if device is not None and not xml_dump:
                package, activity, xml_dump = self._capture_live_ui_snapshot()
            if xml_dump:
                base.with_suffix(".xml").write_text(str(xml_dump), encoding="utf-8")
            metadata = {
                "build": REPLAY_ENGINE_BUILD_ID,
                "serial": self.serial,
                "recording": str(self.recording_dir),
                "step_id": int(step_id),
                "stage": stage,
                "reason": str(reason),
                "foreground_package": package,
                "foreground_activity": activity,
                "captured_at_unix": time.time(),
                "expected_anchor": anchor or {},
                "xml_saved": bool(xml_dump),
            }
            base.with_suffix(".json").write_text(
                json.dumps(metadata, indent=2, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
            if device is not None:
                try:
                    screen = device.screenshot(format="opencv")
                    if screen is not None:
                        cv2.imwrite(str(base.with_suffix(".png")), screen)
                except Exception as screenshot_exc:
                    metadata["screenshot_error"] = str(screenshot_exc)
                    base.with_suffix(".json").write_text(
                        json.dumps(metadata, indent=2, ensure_ascii=False, default=str),
                        encoding="utf-8",
                    )
            print(f"[!] Replay diagnostic artifacts saved: {base}", flush=True)
            return str(base)
        except Exception as exc:
            print(f"[!] Could not save replay diagnostics for step {step_id}: {exc}", flush=True)
            return None

    @staticmethod
    def _step_expected_package(step: Optional[Dict[str, Any]]) -> str:
        if not step:
            return ""
        anchor = step.get("anchor") or {}
        return str(step.get("pre_package") or anchor.get("package") or "").strip()

    def _package_matches_recording(self, expected: str, current: str) -> bool:
        expected = str(expected or "").strip().casefold()
        current = str(current or "").strip().casefold()
        if not expected:
            return True
        if not current:
            return False
        if expected == current:
            return True
        return self._is_launcher_package(expected) and self._is_launcher_package(current)

    def _validate_step_precondition(
        self,
        step: Dict[str, Any],
        package: str,
        activity: str,
        xml_dump: Optional[str],
    ) -> Tuple[bool, str]:
        """Require the recorded app and a stable live dump before coordinate actions."""
        expected_package = self._step_expected_package(step)
        if expected_package and not self._package_matches_recording(expected_package, package):
            return False, (
                f"PRE_ACTION_CONTEXT_MISMATCH: step {step.get('step_id', '?')} expects "
                f"package {expected_package!r}, but the live dump is from {package!r}. "
                "No input was injected."
            )
        if not xml_dump:
            return False, (
                f"PRE_ACTION_DUMP_UNAVAILABLE: step {step.get('step_id', '?')} could not "
                "obtain a package-stable UI hierarchy dump. No input was injected."
            )
        return True, "live package and UI dump verified"

    def _focused_input_is_ready(
        self,
        xml_dump: Optional[str],
        recorded_anchor: Optional[Dict[str, Any]],
    ) -> Tuple[bool, str]:
        """Verify a text step has a focused, enabled editable field before typing."""
        if not xml_dump:
            return False, "TEXT_INPUT_FOCUS_UNVERIFIED: no live hierarchy dump is available."
        try:
            root = ET.fromstring(xml_dump.encode("utf-8"))
        except Exception as exc:
            return False, f"TEXT_INPUT_FOCUS_UNVERIFIED: hierarchy XML is invalid ({exc})."

        editable_nodes = []
        for node in root.iter("node"):
            attrs = node.attrib
            class_name = str(attrs.get("class", "") or "").casefold()
            editable = (
                any(token in class_name for token in ("edittext", "autocompletetextview", "textinputedittext"))
                or str(attrs.get("editable", "false")).casefold() == "true"
            )
            if not editable:
                continue
            if str(attrs.get("enabled", "true")).casefold() == "false":
                continue
            if str(attrs.get("visible-to-user", "true")).casefold() == "false":
                continue
            bounds = str(attrs.get("bounds", "") or "")
            match = re.fullmatch(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]", bounds)
            if not match:
                continue
            rect = tuple(map(int, match.groups()))
            if rect[2] <= rect[0] or rect[3] <= rect[1]:
                continue
            editable_nodes.append((attrs, rect))

        if not editable_nodes:
            return False, "TEXT_INPUT_FOCUS_UNVERIFIED: no enabled visible editable field exists in the live dump."

        anchor = recorded_anchor or {}
        expected_rid = str(anchor.get("resource_id", "") or "").strip().casefold()
        expected_class = str(anchor.get("class_name", "") or "").strip().casefold()
        expected_bounds = anchor.get("bounds") or ()
        try:
            expected_bounds = tuple(int(v) for v in expected_bounds)
            if len(expected_bounds) != 4:
                expected_bounds = ()
        except (TypeError, ValueError):
            expected_bounds = ()

        focused = [item for item in editable_nodes if str(item[0].get("focused", "false")).casefold() == "true"]
        # If the recorder captured a focused field, bind this text payload to that
        # field by ID, class and/or geometry instead of trusting arbitrary focus.
        def matches_recorded(item):
            attrs, rect = item
            rid = str(attrs.get("resource-id", "") or "").strip().casefold()
            klass = str(attrs.get("class", "") or "").strip().casefold()
            if expected_rid and rid:
                if rid == expected_rid or rid.rsplit("/", 1)[-1] == expected_rid.rsplit("/", 1)[-1]:
                    return True
                # ID differs and we have an explicit expected ID: do not type into
                # another editable field just because it is focused.
                return False
            if expected_class and klass and klass != expected_class:
                return False
            if expected_bounds:
                sl, st, sr, sb = expected_bounds
                inter_w = max(0, min(sr, rect[2]) - max(sl, rect[0]))
                inter_h = max(0, min(sb, rect[3]) - max(st, rect[1]))
                area = max(1, (sr - sl) * (sb - st))
                return (inter_w * inter_h) / area >= 0.25
            return True

        candidates = focused or editable_nodes
        matches = [item for item in candidates if matches_recorded(item)]
        if matches:
            chosen = matches[0]
            if not focused and len(editable_nodes) > 1:
                return False, "TEXT_INPUT_FOCUS_UNVERIFIED: several editable fields exist but none is explicitly focused."
            return True, f"focused editable field verified ({chosen[0].get('resource-id', '') or chosen[0].get('class', 'editable')})"

        # Old sessions may not include a field anchor. Permit an unanchored text
        # step only when exactly one editable field is visible and focused.
        if not expected_rid and not expected_bounds and not expected_class and len(editable_nodes) == 1 and len(focused) == 1:
            return True, "single focused editable field verified (legacy recording)"
        return False, "TEXT_INPUT_FOCUS_MISMATCH: the recorded input field is not the live focused editable field."

    def _verify_expected_next_package(
        self,
        step: Dict[str, Any],
        next_step: Optional[Dict[str, Any]],
        package_before_action: str,
        timeout_s: float = 5.0,
    ) -> Tuple[bool, str]:
        """After a tap/key event that should switch apps, wait for the next step's app.

        A MaaTouch write alone is not considered successful navigation. If the
        next recorded step belongs to another package, verify that package reaches
        the foreground before allowing replay to continue.
        """
        expected_next = self._step_expected_package(next_step)
        before = str(package_before_action or "").strip()
        if not next_step or not expected_next or not before:
            return True, "dispatch_confirmed; no cross-package postcondition"
        if expected_next.casefold() == before.casefold():
            return True, "dispatch_confirmed; same-package continuation"

        deadline = time.perf_counter() + max(0.5, float(timeout_s))
        last_package = ""
        while time.perf_counter() < deadline:
            live_package, live_activity, live_xml = self._capture_live_ui_snapshot()
            self._last_live_snapshot = {
                "package": live_package,
                "activity": live_activity,
                "xml": live_xml,
                "captured_at": time.time(),
            }
            last_package = live_package
            if live_package and live_package.casefold() == expected_next.casefold():
                # Require the app to remain foregrounded across a short settling
                # interval so a launcher animation/intermediate surface isn't
                # mistaken for a completed transition.
                time.sleep(0.25)
                verify_package = self._current_package()
                if verify_package.casefold() == expected_next.casefold():
                    return True, f"next-package-verified:{expected_next}"
            time.sleep(0.15)

        reason = (
            f"POST_ACTION_TRANSITION_FAILED: action step {step.get('step_id', '?')} was injected, "
            f"but next step expects package {expected_next!r}; foreground remained {last_package!r}. "
            "The action will not be marked PASSED because the expected app transition was not verified."
        )
        return False, reason

    def _verify_text_payload_observed(
        self,
        payload: str,
        anchor: Optional[Dict[str, Any]],
        timeout_s: float = 1.8,
    ) -> Tuple[Optional[bool], str]:
        """Read the focused field back from live UI dumps after physical typing.

        Returns True when the expected payload is visible, False when a readable
        field persistently contains different text, and None when Android does
        not expose the value (for example password/custom WebView inputs).
        """
        expected = " ".join(unicodedata.normalize("NFKC", str(payload or "")).split()).casefold()
        if not expected:
            return True, "empty payload has nothing to verify"
        anchor = anchor or {}
        target_rid = str(anchor.get("resource_id", "") or "").strip().casefold()
        deadline = time.perf_counter() + max(0.25, float(timeout_s))
        last_readable_values = []
        while time.perf_counter() < deadline:
            package, activity, xml_dump = self._capture_live_ui_snapshot()
            self._last_live_snapshot = {
                "package": package, "activity": activity,
                "xml": xml_dump, "captured_at": time.time(),
            }
            if not xml_dump:
                time.sleep(0.12)
                continue
            try:
                root = ET.fromstring(xml_dump.encode("utf-8"))
            except Exception:
                time.sleep(0.12)
                continue
            values = []
            password_seen = False
            for node in root.iter("node"):
                attrs = node.attrib
                class_name = str(attrs.get("class", "") or "").casefold()
                editable = any(token in class_name for token in (
                    "edittext", "autocompletetextview", "textinputedittext",
                )) or str(attrs.get("editable", "false")).casefold() == "true"
                if not editable:
                    continue
                if str(attrs.get("visible-to-user", "true")).casefold() == "false":
                    continue
                rid = str(attrs.get("resource-id", "") or "").strip().casefold()
                focused = str(attrs.get("focused", "false")).casefold() == "true"
                if target_rid and rid and not (
                    rid == target_rid or rid.rsplit("/", 1)[-1] == target_rid.rsplit("/", 1)[-1]
                ):
                    continue
                if not target_rid and not focused:
                    continue
                if str(attrs.get("password", "false")).casefold() == "true":
                    password_seen = True
                    continue
                value = str(attrs.get("text", "") or "").strip()
                if value:
                    values.append(" ".join(unicodedata.normalize("NFKC", value).split()).casefold())
            if any(expected == value or expected in value for value in values):
                return True, "typed payload read back from live editable node"
            if values:
                last_readable_values = values
            time.sleep(0.12)

        if last_readable_values:
            return False, (
                "TEXT_INPUT_CONTENT_MISMATCH: expected payload was not read back in the editable field; "
                f"last visible values={last_readable_values[:3]!r}."
            )
        return None, "text value is not exposed by the live hierarchy; focus and next-target checks remain authoritative"

    # -----------------------------------------------------------------
    # SAFE POPUP RECOVERY + BOUNDED LIVE SCROLL SEARCH
    # -----------------------------------------------------------------

    _SAFE_DISMISS_LABELS = {
        "not now", "maybe later", "no thanks", "skip", "skip for now",
        "close", "dismiss", "got it", "later", "continue shopping",
        "keep shopping", "keep browsing", "maybe another time",
        "no thank you", "not interested", "remind me later",
        "skip this step", "cancel update",
    }
    _POPUP_PROMPT_HINTS = (
        "rate our app", "rate us", "enable notifications", "turn on notifications",
        "get notifications", "stay updated", "update available", "new version",
        "what's new", "whats new", "choose your location", "select your location",
        "set your location", "try prime", "try amazon prime", "cookie preferences",
        "accept cookies", "privacy choices", "are you enjoying", "app update",
        "limited time offer", "special offer", "sign in for a better",
        "choose your address", "select your address", "set your address",
        "added to cart", "added to your cart", "item added to cart", "cart added",
        "update the app", "update amazon", "sign in to continue", "sign in or create",
        "open in app", "try the new", "allow amazon to send notifications",
    )
    _PROTECTED_ACTION_HINTS = (
        "place order", "buy now", "buy it now", "checkout", "payment", "pay now",
        "confirm purchase", "confirm order", "delete account", "delete item",
        "uninstall", "factory reset", "subscribe now", "start paid", "complete purchase",
        "send money", "transfer money", "authorize payment", "confirm payment",
    )

    def _tap_uia_bounds_safely(self, bounds: Tuple[int, int, int, int]) -> bool:
        """Tap a live hierarchy node through MaaTouch, with guaranteed pointer release."""
        driver = self.maatouch
        if driver is None or not getattr(driver, "_ready", True):
            return False
        left, top, right, bottom = bounds
        if right <= left or bottom <= top:
            return False
        x, y = self._uia_to_touch_point((left + right) // 2, (top + bottom) // 2)
        pointer_id = 0
        try:
            driver.touch_down(
                pointer_id, x, y,
                pressure=min(70, int(getattr(driver, "max_pressure", 255))),
            )
            time.sleep(0.07)
            driver.touch_up(pointer_id)
            time.sleep(0.30)
            return True
        except Exception as exc:
            try:
                driver.touch_up(pointer_id)
            except Exception:
                pass
            print(f"[!] Safe popup dismissal tap failed at ({x}, {y}): {exc}", flush=True)
            return False

    def _recover_safe_blocker(
        self,
        package: str,
        xml_dump: Optional[str],
        expected_package: str = "",
        expected_anchor: Optional[Dict[str, Any]] = None,
        step_id: Optional[int] = None,
    ) -> bool:
        """Dismiss recognized optional blockers; never confirm purchases or grant permissions.

        The method requires dialog/popup evidence or a known blocking prompt and
        only taps an explicit safe dismissal label. If a recognized modal has no
        safe button, Android Back may dismiss the modal without accepting it.
        """
        device = self.u2_dev
        driver = self.maatouch
        if device is None or not xml_dump or driver is None:
            return False

        expected_anchor = expected_anchor or {}
        expected_labels = {
            self._normalise_ui_label(expected_anchor.get(key, ""))
            for key in ("text", "content_desc")
        }
        expected_labels.discard("")
        # Leave a dismissal button to the normal resolver when it is explicitly
        # the next recorded action. Never auto-handle permission/consent actions.
        if expected_labels & self._SAFE_DISMISS_LABELS:
            return False
        if expected_labels & {
            "while using the app", "only this time", "dont allow", "dont allow", "deny",
            "allow", "allow all the time", "allow only while using the app",
        }:
            return False
        expected_semantics = self._normalise_ui_label(" ".join(
            str(expected_anchor.get(key, "") or "")
            for key in ("text", "content_desc", "resource_id")
        ))
        if any(label in expected_semantics for label in self._SAFE_DISMISS_LABELS):
            return False
        if any(word in expected_semantics for word in (
            "allow", "permission", "agree", "accept", "confirm", "purchase", "order", "pay"
        )):
            return False

        try:
            root = ET.fromstring(xml_dump.encode("utf-8"))
        except Exception:
            return False

        nodes = list(root.iter("node"))
        parent_map = {child: parent for parent in nodes for child in list(parent)}
        labels: List[str] = []
        modal_nodes = []
        for node in nodes:
            attrs = node.attrib
            raw_label = " ".join((attrs.get("text", ""), attrs.get("content-desc", ""))).strip()
            if raw_label:
                labels.append(self._normalise_ui_label(raw_label))
            class_id = " ".join((attrs.get("class", ""), attrs.get("resource-id", ""))).casefold()
            visible = str(attrs.get("visible-to-user", "true")).casefold() != "false"
            if visible and any(token in class_id for token in (
                "alertdialog", "dialog", "popupwindow", "popup_window", "bottom_sheet",
                "bottomsheet", "modal", "overlay_dialog", "dialog_container",
            )):
                modal_nodes.append(node)

        full_text = " ".join(labels)
        current_package_lower = str(package or "").casefold()
        is_android_permission_ui = current_package_lower in {
            "com.android.permissioncontroller", "com.google.android.permissioncontroller",
            "com.android.packageinstaller", "com.google.android.packageinstaller",
            "com.android.systemui",
        }
        prompt_present = any(hint in full_text for hint in self._POPUP_PROMPT_HINTS)
        if not modal_nodes and not prompt_present and not is_android_permission_ui:
            return False

        # If the next recorded target is itself inside the modal (for example
        # Amazon's "View Cart" action in an Added-to-Cart sheet), keep it for the
        # normal target resolver. Do not dismiss the popup ahead of the intended tap.
        target_text = self._normalise_ui_label(expected_anchor.get("text", ""))
        target_desc = self._normalise_ui_label(expected_anchor.get("content_desc", ""))
        target_rid = str(expected_anchor.get("resource_id", "") or "").casefold()
        def target_node_matches(node) -> bool:
            attrs = node.attrib
            texts = [
                self._normalise_ui_label(attrs.get("text", "")),
                self._normalise_ui_label(attrs.get("content-desc", "")),
            ]
            rid = str(attrs.get("resource-id", "") or "").casefold()
            text_match = any(
                desired and candidate and (
                    candidate == desired
                    or (len(desired) >= 4 and desired in candidate)
                    or (len(candidate) >= 6 and candidate in desired)
                )
                for desired in (target_text, target_desc) for candidate in texts
            )
            rid_match = bool(target_rid and rid and (
                rid == target_rid or rid.rsplit("/", 1)[-1] == target_rid.rsplit("/", 1)[-1]
            ))
            return text_match or rid_match
        for node in nodes:
            if not target_node_matches(node):
                continue
            parent = node
            while parent is not None:
                if parent in modal_nodes:
                    return False
                parent = parent_map.get(parent)

        # Exact negative/close choices are safe even when product content behind
        # the overlay contains Buy Now/Checkout. For a Back-only fallback, be much
        # more conservative and refuse if the modal itself appears consequential.
        candidates = []
        for node in nodes:
            if modal_nodes:
                ancestor = node
                inside_modal = False
                while ancestor is not None:
                    if ancestor in modal_nodes:
                        inside_modal = True
                        break
                    ancestor = parent_map.get(ancestor)
                if not inside_modal:
                    continue
            attrs = node.attrib
            node_labels = [
                self._normalise_ui_label(attrs.get("text", "")),
                self._normalise_ui_label(attrs.get("content-desc", "")),
            ]
            safe_label = next((label for label in node_labels if label in self._SAFE_DISMISS_LABELS), "")
            if not safe_label:
                continue
            action_node = node
            current = node
            for _ in range(5):
                a = current.attrib
                bounds_match = re.fullmatch(
                    r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]",
                    str(a.get("bounds", "")),
                )
                interactive = any(str(a.get(key, "false")).casefold() == "true"
                                  for key in ("clickable", "long-clickable", "focusable"))
                enabled = str(a.get("enabled", "true")).casefold() != "false"
                visible = str(a.get("visible-to-user", "true")).casefold() != "false"
                if bounds_match and interactive and enabled and visible:
                    action_node = current
                    break
                if current not in parent_map:
                    action_node = None
                    break
                current = parent_map[current]
            if action_node is None:
                continue
            a = action_node.attrib
            bounds_match = re.fullmatch(
                r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]",
                str(a.get("bounds", "")),
            )
            if not bounds_match:
                continue
            bounds = tuple(map(int, bounds_match.groups()))
            if bounds[2] <= bounds[0] or bounds[3] <= bounds[1]:
                continue
            area = (bounds[2] - bounds[0]) * (bounds[3] - bounds[1])
            candidates.append((area, safe_label, bounds))

        if candidates:
            candidates.sort(key=lambda item: item[0])
            _, label, bounds = candidates[0]
            print(
                f"[*] Safe blocker recovery on step {step_id or '?'}: "
                f"dismissing recognized popup via {label!r} at bounds={bounds}, "
                f"foreground={package!r}.",
                flush=True,
            )
            return self._tap_uia_bounds_safely(bounds)

        modal_text_parts = []
        for node in nodes:
            ancestor = node
            inside_modal = False
            while ancestor is not None:
                if ancestor in modal_nodes:
                    inside_modal = True
                    break
                ancestor = parent_map.get(ancestor)
            if inside_modal:
                value = " ".join((node.attrib.get("text", ""), node.attrib.get("content-desc", ""))).strip()
                if value:
                    modal_text_parts.append(self._normalise_ui_label(value))
        modal_text = " ".join(modal_text_parts) if modal_nodes else full_text
        protected_modal = any(hint in modal_text for hint in self._PROTECTED_ACTION_HINTS)
        if (modal_nodes or is_android_permission_ui) and not protected_modal and (is_android_permission_ui or not expected_package or package.casefold() == expected_package.casefold()):
            try:
                print(
                    f"[*] Safe blocker recovery on step {step_id or '?'}: "
                    f"pressing Back to dismiss modal without accepting it; foreground={package!r}.",
                    flush=True,
                )
                device.press("back")
                time.sleep(0.35)
                return True
            except Exception as exc:
                print(f"[!] Could not dismiss modal with Back: {exc}", flush=True)
        return False

    def _scroll_live_content_for_target(
        self,
        xml_dump: Optional[str],
        expected_norm_xy: Tuple[float, float],
        anchor: Optional[Dict[str, Any]] = None,
        attempt_index: int = 0,
    ) -> bool:
        """Perform one bounded live scroll to expose an off-screen expected target."""
        device = self.u2_dev
        if device is None:
            return False
        anchor = anchor or {}
        target_label = self._normalise_ui_label(
            anchor.get("text") or anchor.get("content_desc") or anchor.get("resource_id") or ""
        )
        # Bottom navigation controls are fixed, not list content; scrolling won't
        # reveal them and can hide the actual blocker instead.
        static_nav_labels = {"cart", "basket", "home", "search", "account", "profile", "you"}
        if target_label in static_nav_labels and expected_norm_xy[1] >= 0.86:
            return False
        try:
            ui_w, ui_h = self._uia_dimensions()
            root = ET.fromstring(xml_dump.encode("utf-8")) if xml_dump else None
        except Exception:
            return False
        if root is None:
            return False

        # Never scroll through an unresolved modal: it may be a consequential
        # confirmation or a system prompt. Dismiss it safely first or fail visibly.
        for node in root.iter("node"):
            attrs = node.attrib
            if str(attrs.get("visible-to-user", "true")).casefold() == "false":
                continue
            class_id = " ".join((attrs.get("class", ""), attrs.get("resource-id", ""))).casefold()
            if any(token in class_id for token in (
                "alertdialog", "dialog", "popupwindow", "popup_window", "bottom_sheet",
                "bottomsheet", "modal", "overlay_dialog", "dialog_container",
            )):
                return False

        candidates = []
        for node in root.iter("node"):
            attrs = node.attrib
            if str(attrs.get("scrollable", "false")).casefold() != "true":
                continue
            if str(attrs.get("enabled", "true")).casefold() == "false":
                continue
            package_name = str(attrs.get("package", "") or "").casefold()
            class_id = " ".join((attrs.get("class", ""), attrs.get("resource-id", ""))).casefold()
            if "inputmethod" in package_name or "keyboard" in class_id or "suggestion_strip" in class_id:
                continue
            match = re.fullmatch(
                r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]",
                str(attrs.get("bounds", "")),
            )
            if not match:
                continue
            left, top, right, bottom = map(int, match.groups())
            width, height = right - left, bottom - top
            if width < ui_w * 0.30 or height < ui_h * 0.18:
                continue
            if width * height > ui_w * ui_h * 0.96:
                continue
            if bottom <= ui_h * 0.25 or top >= ui_h * 0.93:
                continue
            px, py = expected_norm_xy[0] * ui_w, expected_norm_xy[1] * ui_h
            contains_expected = left <= px <= right and top <= py <= bottom
            area = width * height
            candidates.append((1 if contains_expected else 0, area, (left, top, right, bottom)))

        if candidates:
            candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
            left, top, right, bottom = candidates[0][2]
        else:
            # Some WebViews/custom lists omit scrollable=true. Use only the
            # central content viewport, excluding system bars and bottom nav.
            if not (0.06 <= expected_norm_xy[1] <= 0.88):
                return False
            left, top, right, bottom = int(ui_w * 0.06), int(ui_h * 0.10), int(ui_w * 0.94), int(ui_h * 0.88)

        center_x = (left + right) // 2
        usable_h = max(1, bottom - top)
        if attempt_index % 2 == 0:
            # Forward in a list: swipe upward to reveal items farther down.
            start_y = int(top + usable_h * 0.78)
            end_y = int(top + usable_h * 0.28)
            direction = "up/forward"
        else:
            # Reverse once to search the preceding part of the viewport.
            start_y = int(top + usable_h * 0.28)
            end_y = int(top + usable_h * 0.78)
            direction = "down/backward"
        start_y = max(1, min(ui_h - 2, start_y))
        end_y = max(1, min(ui_h - 2, end_y))
        try:
            print(
                f"[*] Bounded target-search scroll {attempt_index + 1}/2 ({direction}) "
                f"inside {(left, top, right, bottom)} for {target_label or 'the next target'!r}.",
                flush=True,
            )
            device.swipe(center_x, start_y, center_x, end_y, duration=0.38)
            time.sleep(0.45)
            return True
        except Exception as exc:
            print(f"[!] Bounded target-search scroll failed: {exc}", flush=True)
            return False

    def _verify_expected_next_target(
        self,
        next_step: Optional[Dict[str, Any]],
        package_before_action: str,
        snapshot_before_action: Optional[Dict[str, Any]] = None,
        timeout_s: float = 8.0,
    ) -> Tuple[bool, str]:
        """Verify the next target; recover only from bounded safe blockers/scrolls.

        A fresh stable dump is required after each recovery. The function will
        not declare success from an old pre-action dump and will not click
        consequential buttons to dismiss an unexpected prompt.
        """
        if not next_step or str(next_step.get("event_type", "")).upper() not in {"TAP", "LONG_PRESS"}:
            return True, "no following semantic tap target"
        anchor = next_step.get("anchor") or {}
        if not any(str(anchor.get(key, "") or "").strip() for key in ("resource_id", "text", "content_desc")):
            return True, "next step has no semantic anchor; resolver will use its visual evidence"

        rid_tail = str(anchor.get("resource_id", "") or "").rsplit("/", 1)[-1].casefold()
        if not str(anchor.get("text", "") or "").strip() and not str(anchor.get("content_desc", "") or "").strip() and rid_tail in {
            "header", "root", "container", "content", "main", "parent", "layout",
        }:
            return True, "next anchor is a generic container; defer to guarded resolver"

        trajectory = next_step.get("trajectory") or []
        if trajectory:
            sample = trajectory[0]
            try:
                target_xy = (float(sample.get("rx", 0.5)), float(sample.get("ry", 0.5)))
            except (TypeError, ValueError):
                target_xy = (0.5, 0.5)
        else:
            target_xy = (0.5, 0.5)
        target_xy = (max(0.0, min(1.0, target_xy[0])), max(0.0, min(1.0, target_xy[1])))

        expected_package = self._step_expected_package(next_step)
        before_snapshot = snapshot_before_action or {}
        before_package = str(before_snapshot.get("package") or package_before_action or "").strip()
        # A pre-action match is logged for diagnostics only. It is not sufficient
        # to pass a post-action verification because a popup may have appeared.
        before_xml = before_snapshot.get("xml")
        before_visible = False
        if before_xml and (not expected_package or not before_package or expected_package.casefold() == before_package.casefold()):
            before_visible = self._find_bounds_in_hierarchy(anchor, target_xy, xml_string=str(before_xml)) is not None

        deadline = time.perf_counter() + max(2.0, float(timeout_s))
        last_package = before_package
        blocker_recoveries = 0
        scroll_recoveries = 0
        last_recovery_at = 0.0
        last_xml: Optional[str] = None
        while time.perf_counter() < deadline:
            live_package, live_activity, live_xml = self._capture_live_ui_snapshot()
            self._last_live_snapshot = {
                "package": live_package, "activity": live_activity,
                "xml": live_xml, "captured_at": time.time(),
            }
            last_package, last_xml = live_package, live_xml

            # Recover an optional popup before testing visibility: the target can
            # remain in the XML behind an overlay and must not count as reachable.
            if blocker_recoveries < 2 and live_xml:
                if self._recover_safe_blocker(
                    live_package, live_xml, expected_package, anchor,
                    step_id=next_step.get("step_id"),
                ):
                    blocker_recoveries += 1
                    time.sleep(0.25)
                    continue

            package_matches = not expected_package or (
                live_package and live_package.casefold() == expected_package.casefold()
            )
            if live_xml and package_matches:
                bounds = self._find_bounds_in_hierarchy(anchor, target_xy, xml_string=live_xml)
                if bounds is not None and bounds != (-1, -1, -1, -1):
                    time.sleep(0.12)
                    verify_package, verify_activity, verify_xml = self._capture_live_ui_snapshot()
                    self._last_live_snapshot = {
                        "package": verify_package, "activity": verify_activity,
                        "xml": verify_xml, "captured_at": time.time(),
                    }
                    if verify_xml and (not expected_package or verify_package.casefold() == expected_package.casefold()):
                        # Run blocker check on the confirming dump too. A modal
                        # layered over the target is still an obstruction.
                        if blocker_recoveries < 2 and self._recover_safe_blocker(
                            verify_package, verify_xml, expected_package, anchor,
                            step_id=next_step.get("step_id"),
                        ):
                            blocker_recoveries += 1
                            time.sleep(0.25)
                            continue
                        if self._find_bounds_in_hierarchy(anchor, target_xy, xml_string=verify_xml) is not None:
                            return True, "next-target-visible-after-action-and-recovery"

            elapsed = time.perf_counter() - deadline + max(2.0, float(timeout_s))
            # Scroll at most twice. Keep enough timeout for one fresh dump after
            # each swipe, and avoid repeated swiping while the animation settles.
            if scroll_recoveries < 2 and live_xml and elapsed >= 0.7 and time.perf_counter() - last_recovery_at >= 0.65:
                if self._scroll_live_content_for_target(
                    live_xml, target_xy, anchor, attempt_index=scroll_recoveries,
                ):
                    scroll_recoveries += 1
                    last_recovery_at = time.perf_counter()
                    continue
            time.sleep(0.18)

        expected_label = str(anchor.get("text") or anchor.get("content_desc") or anchor.get("resource_id") or "<visual target>")
        return False, (
            f"NEXT_TARGET_NOT_READY: next expected element {expected_label!r} did not appear "
            f"in fresh UI dumps after safe blocker recovery={blocker_recoveries} and "
            f"bounded scrolls={scroll_recoveries}; expected package={expected_package!r}, "
            f"last foreground package={last_package!r}, target was visible before action={before_visible}."
        )

    def _launch_expected_app(self, package_name: str, timeout_s: float = 3.5) -> bool:
        """Recover when a recorded app is expected but a previous tap left us elsewhere.

        This recovery launches only the exact package named by the recording and
        verifies that it reaches the foreground. It never taps a coordinate to
        guess an app icon. Launcher targets are intentionally excluded.
        """
        package_name = str(package_name or "").strip()
        device = self.u2_dev
        if not package_name or device is None or self._is_launcher_package(package_name):
            return False
        current_package = self._current_package().casefold()
        # Do not relaunch an app over Android permission/install prompts. Those
        # screens may be legitimate recorded steps and must be resolved explicitly.
        if current_package in {
            "com.android.permissioncontroller",
            "com.google.android.permissioncontroller",
            "com.android.packageinstaller",
            "com.google.android.packageinstaller",
            "com.android.systemui",
        }:
            print(
                f"[!] App-context recovery paused on Android system screen "
                f"{current_package!r}; resolve the recorded system step first.",
                flush=True,
            )
            return False
        if package_name.casefold() in {
            "android",
            "com.android.systemui",
            "com.android.permissioncontroller",
            "com.google.android.permissioncontroller",
            "com.android.packageinstaller",
            "com.google.android.packageinstaller",
            "com.android.documentsui",
            "com.google.android.documentsui",
        }:
            return False

        app_info = getattr(device, "app_info", None)
        if callable(app_info):
            try:
                app_info(package_name)
            except Exception as exc:
                print(
                    f"[!] App-context recovery unavailable: recorded package "
                    f"{package_name!r} is not installed/readable ({exc}).",
                    flush=True,
                )
                return False

        try:
            print(
                f"[!] App-context recovery: launching recorded app "
                f"{package_name!r} because the foreground app is different.",
                flush=True,
            )
            device.app_start(package_name, stop=False)
        except Exception as exc:
            print(
                f"[!] App-context recovery could not launch {package_name!r}: {exc}",
                flush=True,
            )
            return False

        deadline = time.perf_counter() + max(0.5, float(timeout_s))
        while time.perf_counter() < deadline:
            if self._current_package().casefold() == package_name.casefold():
                print(
                    f"[+] App-context recovery confirmed foreground "
                    f"{package_name!r}.",
                    flush=True,
                )
                time.sleep(0.25)  # let first-frame/layout animations settle
                return True
            time.sleep(0.15)

        print(
            f"[!] App-context recovery timed out: expected {package_name!r}, "
            f"current {self._current_package()!r}.",
            flush=True,
        )
        return False

    def _find_interactive_point_at_normalized(
        self,
        expected_norm_xy: Tuple[float, float],
    ) -> Optional[Tuple[int, int]]:
        """Return the recorded point only if it lands inside a live interactive node.

        Used solely when a launcher package in an older recording is stale after
        an app transition. This is a guarded hit-test, not blind coordinate replay.
        """
        device = self.u2_dev
        driver = self.maatouch
        if device is None or driver is None:
            return None
        try:
            rx, ry = float(expected_norm_xy[0]), float(expected_norm_xy[1])
            if not (math.isfinite(rx) and math.isfinite(ry) and 0.0 <= rx <= 1.0 and 0.0 <= ry <= 1.0):
                return None
            xml_string = device.dump_hierarchy(compressed=False)
            root = ET.fromstring(xml_string.encode("utf-8"))
            ui_w, ui_h = self._uia_dimensions()
            point_x = min(ui_w - 1, max(0, int(round(rx * ui_w))))
            point_y = min(ui_h - 1, max(0, int(round(ry * ui_h))))
        except Exception:
            return None

        candidates = []
        screen_area = max(1, ui_w * ui_h)
        for element in root.iter("node"):
            attrs = element.attrib
            actionable = any(
                str(attrs.get(key, "false")).strip().casefold() == "true"
                for key in ("clickable", "focusable", "long-clickable")
            )
            if not actionable:
                continue
            match = re.fullmatch(
                r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]",
                str(attrs.get("bounds", "") or ""),
            )
            if not match:
                continue
            left, top, right, bottom = map(int, match.groups())
            if right <= left or bottom <= top:
                continue
            # Exclude full-screen/container nodes; a small interactive node
            # containing the recorded point gives a much stronger hit test.
            area = (right - left) * (bottom - top)
            if area > screen_area * 0.65:
                continue
            if not (left <= point_x <= right and top <= point_y <= bottom):
                continue
            candidates.append((area, (left, top, right, bottom), attrs))

        if not candidates:
            return None
        candidates.sort(key=lambda item: (item[0], 0 if item[2].get("clickable", "").casefold() == "true" else 1))
        # Preserve the actual recorded point, instead of tapping an element centre.
        touch_x = round(rx * max(1, int(driver.max_x) - 1))
        touch_y = round(ry * max(1, int(driver.max_y) - 1))
        return (
            max(0, min(int(driver.max_x) - 1, touch_x)),
            max(0, min(int(driver.max_y) - 1, touch_y)),
        )

    @staticmethod
    def _is_launcher_package(package_name: str) -> bool:
        """Recognize common Android home/launcher packages across brands."""
        value = str(package_name or "").strip().casefold()
        return bool(value) and any(
            token in value
            for token in (
                "launcher",
                "miui.home",
                "nexuslauncher",
                "sec.android.app",
                "com.realme.home",
                "oplus.home",
            )
        )

    def _normalized_fallback_is_safe(
        self,
        anchor: Dict[str, Any],
        expected_package: str = "",
    ) -> bool:
        """Permit coordinate-only replay only when the foreground app is known."""
        expected = str(
            expected_package or anchor.get("package", "") or ""
        ).strip()
        current = self._current_package()

        if not expected or not current:
            print(
                "[!] Coordinate-only fallback blocked: expected/current "
                "foreground package is unavailable.",
                flush=True,
            )
            return False

        if expected.casefold() != current.casefold():
            print(
                "[!] Coordinate-only fallback blocked: expected app "
                f"{expected!r}, but current app is {current!r}.",
                flush=True,
            )
            return False

        return True

    def _wait_and_resolve_target(
        self,
        anchor: Dict[str, Any],
        first_sample: Dict[str, Any],
        timeout_s: float = 6.0,
        scroll_recovery_trajectory: Optional[List[Dict[str, Any]]] = None,
        expected_package: str = "",
        step_id: Optional[int] = None,
    ) -> Tuple[Optional[Tuple[int, int]], str]:
        """Resolve target safely: hierarchy, visual template, then coordinate-only fallback.

        Important safety rule:
        A semantic anchor that is not found must NEVER fall back to its old
        coordinates merely because the package name matches. Launcher pages,
        dialogs, and app drawers share the same package while having different
        layouts; that behavior caused taps to land on unrelated items.
        """
        started = time.perf_counter()
        deadline = started + max(0.5, float(timeout_s))

        raw_rx = first_sample.get("rx")
        raw_ry = first_sample.get("ry")
        has_valid_normalized_target = False
        try:
            expected_rx = float(raw_rx)
            expected_ry = float(raw_ry)
            has_valid_normalized_target = (
                math.isfinite(expected_rx)
                and math.isfinite(expected_ry)
                and 0.0 <= expected_rx <= 1.0
                and 0.0 <= expected_ry <= 1.0
            )
        except (TypeError, ValueError):
            expected_rx, expected_ry = 0.5, 0.5

        # Clamp only after validation; edge taps are valid recorded targets.
        expected_rx = max(0.0, min(1.0, expected_rx))
        expected_ry = max(0.0, min(1.0, expected_ry))
        expected_norm_xy = (expected_rx, expected_ry)

        target_package = str(
            expected_package or anchor.get("package", "") or ""
        ).strip()
        has_semantic_anchor = bool(
            str(anchor.get("resource_id", "") or "").strip()
            or str(anchor.get("text", "") or "").strip()
            or str(anchor.get("content_desc", "") or "").strip()
        )
        crop_relative = str(anchor.get("visual_crop_path", "") or "").strip()
        crop_full = (self.recording_dir / crop_relative) if crop_relative else None
        has_visual_reference = bool(crop_full and crop_full.is_file())

        scroll_retries = 1 if scroll_recovery_trajectory else 0
        last_context_message = ""
        last_geometry_check_elapsed = -1.0
        expected_app_recovery_attempted = False
        logged_live_dump = False
        blocker_recoveries = 0
        generic_scroll_recoveries = 0
        last_generic_scroll_at = 0.0

        while time.perf_counter() < deadline:
            # Verify the foreground app and hierarchy as one live snapshot BEFORE
            # inspecting targets or injecting anything.
            # anything. This prevents a same-looking control in another app
            # from being tapped after an unexpected app/launcher transition.
            current_package, current_activity, current_xml = self._capture_live_ui_snapshot()
            self._last_live_snapshot = {
                "package": current_package,
                "activity": current_activity,
                "xml": current_xml,
                "captured_at": time.time(),
            }
            if not logged_live_dump:
                node_count = current_xml.count("<node") if current_xml else 0
                print(
                    f"[*] Fresh UI dump before step {step_id if step_id is not None else '?'}: "
                    f"package={current_package!r}, activity={current_activity!r}, "
                    f"nodes={node_count}, stable_xml={bool(current_xml)}.",
                    flush=True,
                )
                logged_live_dump = True
            # If an unexpected optional modal is covering the next recorded
            # target, close only a recognized safe dismissal choice. Do not tap
            # Allow/Agree/Buy/Pay/Confirm buttons implicitly.
            if blocker_recoveries < 2 and current_xml:
                if self._recover_safe_blocker(
                    current_package, current_xml, target_package, anchor, step_id=step_id,
                ):
                    blocker_recoveries += 1
                    time.sleep(0.25)
                    continue
            different_packages = bool(
                target_package
                and current_package
                and target_package.casefold() != current_package.casefold()
            )
            if target_package and not current_package:
                last_context_message = (
                    f"foreground package unavailable; expected {target_package!r}"
                )
                grace = min(1.5, max(0.4, float(timeout_s) * 0.35))
                if time.perf_counter() - started >= grace:
                    return None, "APP_CONTEXT_UNKNOWN: " + last_context_message
                time.sleep(0.2)
                continue

            cross_brand_launcher = (
                different_packages
                and self._is_launcher_package(target_package)
                and self._is_launcher_package(current_package)
            )
            if different_packages and not cross_brand_launcher:
                elapsed = time.perf_counter() - started
                last_context_message = (
                    f"foreground package mismatch: expected {target_package!r}, "
                    f"current {current_package!r}"
                )

                # Older recorder sessions can retain the launcher package/XML
                # for a touch made just after an app transition. In that case
                # the semantic anchor is stale, but the normalized recorded
                # point may still identify a real interactive node in the live
                # app. Only accept it for a recorded launcher->app mismatch and
                # only after a successful live hit-test at that exact point.
                launcher_anchor_stale = (
                    self._is_launcher_package(target_package)
                    and not self._is_launcher_package(current_package)
                    and (
                        str(anchor.get("package", "") or "").casefold() == target_package.casefold()
                        or str(anchor.get("resource_id", "") or "").casefold().startswith(target_package.casefold() + ":id/")
                    )
                )
                if launcher_anchor_stale and elapsed >= 0.45:
                    live_point = self._find_interactive_point_at_normalized(expected_norm_xy)
                    if live_point is not None:
                        print(
                            "[!] Recorded package/anchor appears stale after an app transition; "
                            "using the recorded point only after a live interactive-node hit-test "
                            f"in foreground {current_package!r} at {live_point}.",
                            flush=True,
                        )
                        return live_point, "TIER2C_STALE_LAUNCHER_ANCHOR_GUARDED_HIT_TEST"

                # If the recording clearly expects an installed app but a prior
                # launcher tap did not open it (or another app opened instead),
                # explicitly bring that recorded app to foreground, verify the
                # package, and then retry normal semantic target resolution.
                can_recover_app = (
                    bool(target_package)
                    and not self._is_launcher_package(target_package)
                    and elapsed >= 0.9
                    and not expected_app_recovery_attempted
                )
                if can_recover_app:
                    expected_app_recovery_attempted = True
                    if self._launch_expected_app(target_package, timeout_s=min(3.5, max(1.0, deadline - time.perf_counter()))):
                        time.sleep(0.15)
                        continue

                # If no safe recovery applied, wait briefly for normal Android
                # launch/transition animation before failing.
                grace = min(2.5, max(0.8, float(timeout_s) * 0.4))
                if elapsed >= grace:
                    print(
                        f"[!] Target resolution stopped: {last_context_message}",
                        flush=True,
                    )
                    return None, "APP_CONTEXT_MISMATCH: " + last_context_message
                time.sleep(0.2)
                continue

            if not current_xml:
                time.sleep(0.15)
                continue

            bounds = self._find_bounds_in_hierarchy(
                anchor, expected_norm_xy, xml_string=current_xml
            )
            # Older implementations used this sentinel to mean an automatic
            # dialog tap had occurred. Dialogs are no longer tapped implicitly;
            # treat the sentinel as no target found.
            if bounds == (-1, -1, -1, -1):
                time.sleep(0.2)
                continue

            if bounds:
                left, top, right, bottom = bounds
                try:
                    rx = max(0.0, min(1.0, float(anchor.get("element_rx", 0.5))))
                    ry = max(0.0, min(1.0, float(anchor.get("element_ry", 0.5))))
                except (TypeError, ValueError):
                    rx = ry = 0.5
                logical_x = int(left + rx * max(1, right - left))
                logical_y = int(top + ry * max(1, bottom - top))
                touch_xy = self._uia_to_touch_point(logical_x, logical_y)
                target_label = str(
                    anchor.get("text") or anchor.get("content_desc")
                    or anchor.get("resource_id") or "<recorded coordinate target>"
                )
                print(
                    f"[+] Fresh dump matched target={target_label!r}, "
                    f"bounds={(left, top, right, bottom)}, tap={touch_xy}.",
                    flush=True,
                )
                return touch_xy, "TIER1_UI_ANCHOR"

            if has_visual_reference:
                crop_mode = str(anchor.get("visual_crop_mode", "") or "").strip().lower()
                crop_rx = anchor.get("visual_crop_anchor_rx")
                crop_ry = anchor.get("visual_crop_anchor_ry")
                if crop_mode == "point":
                    crop_rx = 0.5 if crop_rx is None else crop_rx
                    crop_ry = 0.5 if crop_ry is None else crop_ry
                elif crop_mode == "element":
                    crop_rx = anchor.get("element_rx", 0.5) if crop_rx is None else crop_rx
                    crop_ry = anchor.get("element_ry", 0.5) if crop_ry is None else crop_ry
                else:
                    # Legacy 140x140 templates were point patches, even though
                    # the recording also stored a larger accessibility bounds box.
                    crop_rx, crop_ry = self._legacy_crop_anchor_offset(crop_full, anchor)
                visual_hit = self._match_template_multiscale(
                    crop_full,
                    anchor_rx=crop_rx,
                    anchor_ry=crop_ry,
                    expected_rx=expected_rx,
                    expected_ry=expected_ry,
                )
                if visual_hit:
                    print(
                        f"[*] Target resolved visually at {visual_hit}.",
                        flush=True,
                    )
                    return visual_hit, "TIER2_VISUAL_TEMPLATE"

            elapsed = time.perf_counter() - started

            # Conservative fallback for apps whose controls are rendered without
            # reliable labels. It is only allowed for a live interactive node
            # that substantially overlaps the recorded target and contains the
            # recorded touch point, with the expected app in the foreground.
            if (
                has_semantic_anchor
                and elapsed >= 1.0
                and (last_geometry_check_elapsed < 0.0 or elapsed - last_geometry_check_elapsed >= 1.0)
            ):
                last_geometry_check_elapsed = elapsed
                geometry_hit = self._find_interactive_bounds_near_recorded_target(
                    anchor, expected_norm_xy
                )
                if geometry_hit is not None:
                    left, top, right, bottom = geometry_hit
                    try:
                        rel_x = max(0.0, min(1.0, float(anchor.get("element_rx", 0.5))))
                        rel_y = max(0.0, min(1.0, float(anchor.get("element_ry", 0.5))))
                    except (TypeError, ValueError):
                        rel_x = rel_y = 0.5
                    logical_x = int(left + rel_x * max(1, right - left))
                    logical_y = int(top + rel_y * max(1, bottom - top))
                    touch_xy = self._uia_to_touch_point(logical_x, logical_y)
                    print(
                        "[*] Target resolved by guarded geometry match: "
                        f"{touch_xy} (live interactive bounds match recorded target).",
                        flush=True,
                    )
                    return touch_xy, "TIER2B_VERIFIED_INTERACTIVE_GEOMETRY"

            # If the recorder deliberately omitted a stale package/hierarchy,
            # a visual-only step may still progress when the recorded point is
            # inside a live interactive node. This fallback is guarded by a
            # fresh UI hierarchy hit-test; it is not a blind coordinate tap.
            visual_only_hit_safe = (
                not target_package
                and not has_semantic_anchor
                and has_valid_normalized_target
                and bool(current_package)
                and elapsed >= 1.0
            )
            if visual_only_hit_safe:
                live_point = self._find_interactive_point_at_normalized(expected_norm_xy)
                if live_point is not None:
                    print(
                        "[!] Recording had no fresh package/semantic anchor; "
                        "using recorded point only after a live interactive-node "
                        f"hit-test in foreground {current_package!r} at {live_point}.",
                        flush=True,
                    )
                    return live_point, "TIER2C_VISUAL_ONLY_GUARDED_HIT_TEST"

            # A tap with no text/description/resource-id is coordinate-defined
            # by the recording itself. If the visual crop is dynamic and fails
            # to match, use the recorded normalized point after a brief settle
            # period—but only while the exact expected app is foregrounded.
            # This is what permits Step 6 (empty semantic anchor + crop miss)
            # to continue without making stale-coordinate taps into other apps.
            coordinate_only_safe = (
                not has_semantic_anchor
                and has_valid_normalized_target
                and bool(target_package)
                and bool(current_package)
                and target_package.casefold() == current_package.casefold()
            )
            fallback_after = min(1.5, max(0.8, float(timeout_s) * 0.25))
            if coordinate_only_safe and elapsed >= fallback_after:
                live_point = self._find_interactive_point_at_normalized(expected_norm_xy)
                if live_point is not None:
                    crop_status = (
                        "visual crop did not match" if has_visual_reference else
                        "visual crop missing" if crop_relative else
                        "no visual crop recorded"
                    )
                    print(
                        f"[!] Coordinate fallback gated by fresh live hierarchy: {crop_status}; "
                        f"foreground package verified ({target_package!r}); "
                        f"interactive hit-test={live_point}.",
                        flush=True,
                    )
                    return live_point, "TIER3_COORDINATE_ONLY_GUARDED_INTERACTIVE_HIT_TEST"

            if (
                scroll_retries > 0
                and scroll_recovery_trajectory
                and elapsed >= 1.5
            ):
                scroll_retries -= 1
                width = max(1, int(self.maatouch.max_x))
                height = max(1, int(self.maatouch.max_y))
                first_point = scroll_recovery_trajectory[0]
                print(
                    "[*] Target not found; trying one recorded scroll "
                    "recovery before failing.",
                    flush=True,
                )
                self._inject_scaled_trajectory(
                    int(float(first_point.get("rx", 0.5)) * width),
                    int(float(first_point.get("ry", 0.5)) * height),
                    scroll_recovery_trajectory,
                    gesture_type="SWIPE",
                )
                time.sleep(0.45)
            elif (
                generic_scroll_recoveries < 2
                and elapsed >= 1.0
                and time.perf_counter() - last_generic_scroll_at >= 0.65
            ):
                if self._scroll_live_content_for_target(
                    current_xml, expected_norm_xy, anchor,
                    attempt_index=generic_scroll_recoveries,
                ):
                    generic_scroll_recoveries += 1
                    last_generic_scroll_at = time.perf_counter()
                    continue
                time.sleep(0.20)
            else:
                time.sleep(0.25)

        # A failed semantic target must not be tapped at stale coordinates.
        # For an anchorless action, the recorded normalized position is the
        # target definition; its screenshot crop is a matching hint, not a hard
        # requirement. When the crop changes (dynamic content, antialiasing,
        # app version), allow that original coordinate only if the exact
        # recorded foreground package is still active.
        if has_semantic_anchor:
            return None, (
                "SEMANTIC_TARGET_NOT_FOUND: target text/description/resource ID "
                "was not found in the live hierarchy or by visual matching."
            )

        if not has_valid_normalized_target:
            return None, (
                "INVALID_RECORDED_COORDINATES: anchorless step has no valid "
                "normalized rx/ry coordinate."
            )

        if not self._normalized_fallback_is_safe(anchor, target_package):
            return None, (
                "TARGET_NOT_FOUND: visual crop did not match and coordinate "
                "fallback was blocked because the expected app is not verified."
            )

        fallback_x = round(expected_rx * max(1, int(self.maatouch.max_x) - 1))
        fallback_y = round(expected_ry * max(1, int(self.maatouch.max_y) - 1))
        crop_status = (
            "visual crop did not match" if has_visual_reference else
            "visual crop missing" if crop_relative else
            "no visual crop recorded"
        )
        print(
            f"[!] Coordinate-only target fallback: {crop_status}; "
            f"foreground package verified ({target_package}); "
            f"tap=({fallback_x}, {fallback_y}).",
            flush=True,
        )
        return (fallback_x, fallback_y), "TIER3_COORDINATE_ONLY_FOREGROUND_VERIFIED"

    def _find_bounds_in_hierarchy(
        self,
        anchor: Dict[str, Any],
        expected_norm_xy: Tuple[float, float],
        xml_string: Optional[str] = None,
    ) -> Optional[Tuple[int, int, int, int]]:
        """Find the expected target in a fresh UIAutomator hierarchy dump.

        When the resolver supplies xml_string, matching uses the exact hierarchy
        paired with the foreground-package check for this attempt. This prevents
        a race where app_current() describes one screen and dump_hierarchy()
        returns the next screen.
        """
        try:
            device = self.u2_dev
            if device is None:
                return None
            if xml_string is None:
                xml_string = device.dump_hierarchy(compressed=False)
            if not xml_string:
                return None
            root = ET.fromstring(xml_string.encode("utf-8"))
        except Exception:
            return None

        target_rid = str(anchor.get("resource_id", "") or "").strip().casefold()
        target_text = self._normalise_ui_label(anchor.get("text", ""))
        target_desc = self._normalise_ui_label(anchor.get("content_desc", ""))
        target_class = str(anchor.get("class_name", "") or "").strip().casefold()
        target_node_package = str(anchor.get("package", "") or "").strip().casefold()
        recorded_clickable = bool(anchor.get("clickable", False))
        recorded_long_clickable = bool(anchor.get("long_clickable", False))
        recorded_focusable = bool(anchor.get("focusable", False))
        target_bounds = anchor.get("bounds") or ()
        try:
            target_bounds = tuple(float(v) for v in target_bounds)
            if len(target_bounds) != 4:
                target_bounds = ()
        except (TypeError, ValueError):
            target_bounds = ()

        try:
            screen_width, screen_height = self._uia_dimensions()
        except Exception:
            screen_width = max(1, int(self.maatouch.max_x))
            screen_height = max(1, int(self.maatouch.max_y))
        expected_x, expected_y = expected_norm_xy

        # Do not auto-click permission dialogs or generic Continue/Allow/Agree
        # buttons. Dialog choices must be present as recorded replay steps;
        # implicit clicks can hijack navigation and change the tested state.

        best_bounds = None
        best_score = float("-inf")

        def label_score(target: str, candidate: str) -> float:
            if not target or not candidate:
                return 0.0
            if candidate == target:
                return 14.0
            if target in {"cart", "basket"} and candidate in {
                "view cart", "your cart", "shopping cart", "go to cart", "open cart",
                "cart tab", "my cart", "view basket", "your basket",
            }:
                return 12.0
            if target in candidate or candidate in target:
                # Avoid matching tiny generic labels such as "to" or "in".
                shorter = min(len(target), len(candidate))
                if shorter >= 6:
                    return 10.0 + min(1.0, shorter / max(len(target), len(candidate)))
            target_tokens = set(target.split())
            candidate_tokens = set(candidate.split())
            if target_tokens and candidate_tokens:
                overlap = len(target_tokens & candidate_tokens) / len(target_tokens)
                if overlap >= 0.8 and len(target_tokens) >= 3:
                    return 8.0 + overlap
            return 0.0

        for element in root.iter("node"):
            attrs = element.attrib
            resource_id = str(attrs.get("resource-id", "") or "").strip().casefold()
            text_raw = str(attrs.get("text", "") or "").strip()
            desc_raw = str(attrs.get("content-desc", "") or "").strip()
            text = self._normalise_ui_label(text_raw)
            description = self._normalise_ui_label(desc_raw)
            class_name = str(attrs.get("class", "") or "").strip().casefold()
            candidate_package = str(attrs.get("package", "") or "").strip().casefold()

            if str(attrs.get("enabled", "true")).casefold() == "false":
                continue
            if str(attrs.get("visible-to-user", "true")).casefold() == "false":
                continue
            if target_node_package and candidate_package and candidate_package != target_node_package:
                # System keyboard nodes may be exposed under their own package;
                # only allow that cross-package match for an explicitly recorded
                # keyboard target, never for an ordinary app control.
                keyboard_packages = (
                    "com.google.android.inputmethod", "com.android.inputmethod",
                    "com.samsung.android.honeyboard", "com.swiftkey",
                )
                is_keyboard_target = any(k in target_node_package for k in keyboard_packages)
                if not (is_keyboard_target and any(k in candidate_package for k in keyboard_packages)):
                    continue

            text_score = label_score(target_text, text)
            desc_score = label_score(target_desc, description)
            rid_score = 0.0
            if target_rid and resource_id:
                if resource_id == target_rid:
                    rid_score = 12.0
                elif resource_id.rsplit("/", 1)[-1] == target_rid.rsplit("/", 1)[-1]:
                    rid_score = 8.0

            # Anchor fields are alternative signals, not an AND condition: in a
            # changing suggestion list the text may vary while the resource ID and
            # relative position still identify the right row.
            semantic_score = max(text_score, desc_score, rid_score)
            if semantic_score == 0.0:
                continue

            clickable = str(attrs.get("clickable", "false")).casefold() == "true"
            long_clickable = str(attrs.get("long-clickable", "false")).casefold() == "true"
            focusable = str(attrs.get("focusable", "false")).casefold() == "true"
            interactive = clickable or long_clickable or focusable
            # New recordings capture the actual actionable node. If it was
            # recorded clickable/focusable, don't resolve to a disabled label or
            # unrelated wrapper with the same text.
            if (recorded_clickable or recorded_long_clickable or recorded_focusable) and not interactive:
                continue
            if target_class and class_name == target_class:
                semantic_score += 1.0
            if clickable:
                semantic_score += 2.5
            elif long_clickable:
                semantic_score += 2.0
            elif focusable:
                semantic_score += 1.0

            bounds_string = attrs.get("bounds", "")
            match = re.fullmatch(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]", bounds_string)
            if not match:
                continue
            left, top, right, bottom = map(int, match.groups())
            if right <= left or bottom <= top:
                continue

            center_x_norm = ((left + right) / 2.0) / max(1, screen_width)
            center_y_norm = ((top + bottom) / 2.0) / max(1, screen_height)
            distance = math.hypot(center_x_norm - expected_x, center_y_norm - expected_y)
            point_inside = left <= expected_x * screen_width <= right and top <= expected_y * screen_height <= bottom
            target_has_text = bool(target_text or target_desc)
            # An ID-only anchor such as "header" is insufficient evidence if
            # the live node is nowhere near the recorded touch point. This was
            # the source of incorrect header/container taps in older sessions.
            if not target_has_text and rid_score > 0 and not point_inside:
                continue
            # Preserve semantic identity while using recorded normalized location
            # to disambiguate repeated labels, icons and suggestion rows.
            proximity_bonus = max(0.0, 4.0 * (1.0 - min(1.0, distance)))
            if point_inside:
                proximity_bonus += 2.0
            area_ratio = ((right - left) * (bottom - top)) / max(1.0, screen_width * screen_height)
            recorded_area_ratio = 0.0
            if target_bounds:
                recorded_area_ratio = max(0.0, (target_bounds[2] - target_bounds[0]) * (target_bounds[3] - target_bounds[1])) / max(1.0, screen_width * screen_height)
            if area_ratio > 0.70 and recorded_area_ratio <= 0.70:
                # Do not resolve an ordinary icon/text tap to a screen-sized
                # container merely because its resource ID matched.
                if not target_text and not target_desc:
                    continue
                semantic_score -= 5.0
            total_score = semantic_score + proximity_bonus

            if total_score > best_score:
                best_score = total_score
                best_bounds = (left, top, right, bottom)

        return best_bounds

    def _legacy_crop_anchor_offset(
        self, crop_path: Path, anchor: Dict[str, Any]
    ) -> Tuple[float, float]:
        """Infer offsets for old sessions whose crops have no explicit mode."""
        try:
            template = cv2.imread(str(crop_path))
            height, width = template.shape[:2] if template is not None else (0, 0)
        except Exception:
            width = height = 0
        bounds = anchor.get("bounds") or (0, 0, 0, 0)
        try:
            left, top, right, bottom = [int(v) for v in bounds]
            bw, bh = max(0, right - left), max(0, bottom - top)
        except Exception:
            bw = bh = 0
        # Old recorder versions commonly wrote 140x140 point patches while
        # retaining a much larger accessibility bounds rectangle.
        if width and height and bw and bh:
            width_ratio = min(width, bw) / max(width, bw)
            height_ratio = min(height, bh) / max(height, bh)
            if width_ratio < 0.65 or height_ratio < 0.65:
                return 0.5, 0.5
        try:
            return (
                max(0.0, min(1.0, float(anchor.get("element_rx", 0.5)))),
                max(0.0, min(1.0, float(anchor.get("element_ry", 0.5)))),
            )
        except (TypeError, ValueError):
            return 0.5, 0.5

    def _find_interactive_bounds_near_recorded_target(
        self,
        anchor: Dict[str, Any],
        expected_norm_xy: Tuple[float, float],
    ) -> Optional[Tuple[int, int, int, int]]:
        """Return a current interactive node only when its geometry matches.

        Requires a saved bounds rectangle, an exact foreground-package match,
        a clickable/focusable live node containing the expected tap position,
        and substantial overlap with the recorded rectangle. It never taps an
        arbitrary coordinate merely because the package matches.
        """
        expected_package = str(anchor.get("package", "") or "").strip()
        current_package = self._current_package()
        if not expected_package or not current_package or expected_package.casefold() != current_package.casefold():
            return None

        # Narrow the optional driver/device members once, then use these local
        # references throughout this helper. The target resolver is only valid
        # after execute_test() has connected both dependencies.
        driver = self.maatouch
        device = self.u2_dev
        if driver is None or device is None:
            return None

        saved_bounds = anchor.get("bounds")
        if not isinstance(saved_bounds, (list, tuple)) or len(saved_bounds) != 4:
            return None
        try:
            sl, st, sr, sb = [float(v) for v in saved_bounds]
            source_resolution = self.data.get("source_resolution") or {}
            source_w = max(1.0, float(source_resolution.get("width") or driver.max_x))
            source_h = max(1.0, float(source_resolution.get("height") or driver.max_y))
            ui_w, ui_h = self._uia_dimensions()
            rec_rect = (
                sl / source_w, st / source_h, sr / source_w, sb / source_h
            )
        except (TypeError, ValueError):
            return None
        if sr <= sl or sb <= st:
            return None

        expected_x, expected_y = expected_norm_xy
        try:
            xml_string = self.u2_dev.dump_hierarchy(compressed=False)
            root = ET.fromstring(xml_string.encode("utf-8"))
        except Exception:
            return None

        candidates = []
        for element in root.iter("node"):
            attrs = element.attrib
            clickable = str(attrs.get("clickable", "false")).casefold() == "true"
            focusable = str(attrs.get("focusable", "false")).casefold() == "true"
            if not (clickable or focusable):
                continue
            match = re.fullmatch(
                r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]",
                attrs.get("bounds", ""),
            )
            if not match:
                continue
            left, top, right, bottom = map(int, match.groups())
            if right <= left or bottom <= top:
                continue
            live = (
                left / max(1, ui_w), top / max(1, ui_h),
                right / max(1, ui_w), bottom / max(1, ui_h),
            )
            # The recorded point must land inside the live target, with a
            # tiny tolerance for UIAutomator/viewport coordinate rounding.
            if not (
                live[0] - 0.003 <= expected_x <= live[2] + 0.003
                and live[1] - 0.003 <= expected_y <= live[3] + 0.003
            ):
                continue
            inter_w = max(0.0, min(rec_rect[2], live[2]) - max(rec_rect[0], live[0]))
            inter_h = max(0.0, min(rec_rect[3], live[3]) - max(rec_rect[1], live[1]))
            inter = inter_w * inter_h
            rec_area = max(1e-8, (rec_rect[2] - rec_rect[0]) * (rec_rect[3] - rec_rect[1]))
            live_area = max(1e-8, (live[2] - live[0]) * (live[3] - live[1]))
            union = rec_area + live_area - inter
            iou = inter / max(1e-8, union)
            recorded_coverage = inter / rec_area
            live_coverage = inter / live_area
            # Broad enough for slight app layout variation but strict enough
            # to reject unrelated buttons appearing at the same coordinates.
            if iou < 0.25 or recorded_coverage < 0.45 or live_coverage < 0.45:
                continue
            candidates.append((live_area, (left, top, right, bottom), iou))

        if not candidates:
            return None
        # Prefer the smallest verified matching interactive node, avoiding a
        # large clickable screen/container when a proper control is available.
        candidates.sort(key=lambda item: (item[0], -item[2]))
        return candidates[0][1]

    def _match_template_multiscale(
        self,
        crop_path: Path,
        threshold: float = 0.80,
        anchor_rx: float = 0.5,
        anchor_ry: float = 0.5,
        expected_rx: float = 0.5,
        expected_ry: float = 0.5,
    ) -> Optional[Tuple[int, int]]:
        """Find a crop near its recorded screen position and preserve the tap offset."""
        try:
            screen_bgr = self.u2_dev.screenshot(format="opencv")
            template = cv2.imread(str(crop_path))
            if screen_bgr is None or template is None:
                return None

            gray_screen = cv2.cvtColor(screen_bgr, cv2.COLOR_BGR2GRAY)
            gray_template = cv2.cvtColor(template, cv2.COLOR_BGR2GRAY)
            template_height, template_width = gray_template.shape[:2]
            if template_width < 12 or template_height < 12:
                return None

            try:
                rel_x = max(0.0, min(1.0, float(anchor_rx)))
                rel_y = max(0.0, min(1.0, float(anchor_ry)))
            except (TypeError, ValueError):
                rel_x = rel_y = 0.5

            shot_h, shot_w = gray_screen.shape[:2]
            try:
                expected_x = max(0.0, min(1.0, float(expected_rx))) * shot_w
                expected_y = max(0.0, min(1.0, float(expected_ry))) * shot_h
            except (TypeError, ValueError):
                expected_x, expected_y = shot_w * 0.5, shot_h * 0.5

            best_value = -1.0
            best_origin = None
            best_size = None
            best_distance = float("inf")
            # A template far from the recorded tap point is not trustworthy.
            # This prevents matching a same-looking icon elsewhere in the launcher.
            max_distance_x = int(shot_w * 0.14)
            max_distance_y = int(shot_h * 0.14)

            # Preserve a conservative scale range and restrict each candidate
            # to the neighbourhood of the recorded normalized target.
            for scale in np.linspace(0.75, 1.25, 9):
                resized_width = int(template_width * scale)
                resized_height = int(template_height * scale)
                if (
                    resized_width >= shot_w
                    or resized_height >= shot_h
                    or resized_width < 12
                    or resized_height < 12
                ):
                    continue

                resized = cv2.resize(
                    gray_template,
                    (resized_width, resized_height),
                )
                result = cv2.matchTemplate(
                    gray_screen, resized, cv2.TM_CCOEFF_NORMED
                )

                expected_origin_x = round(expected_x - resized_width * rel_x)
                expected_origin_y = round(expected_y - resized_height * rel_y)
                x0 = max(0, expected_origin_x - max_distance_x)
                x1 = min(result.shape[1], expected_origin_x + max_distance_x + 1)
                y0 = max(0, expected_origin_y - max_distance_y)
                y1 = min(result.shape[0], expected_origin_y + max_distance_y + 1)
                if x0 >= x1 or y0 >= y1:
                    continue

                local = result[y0:y1, x0:x1]
                _, max_value, _, local_location = cv2.minMaxLoc(local)
                origin = (x0 + int(local_location[0]), y0 + int(local_location[1]))
                hit_x = origin[0] + round(resized_width * rel_x)
                hit_y = origin[1] + round(resized_height * rel_y)
                distance = math.hypot(
                    (hit_x / max(1, shot_w)) - float(expected_x / max(1, shot_w)),
                    (hit_y / max(1, shot_h)) - float(expected_y / max(1, shot_h)),
                )

                if distance <= 0.20 and float(max_value) > best_value:
                    best_value = float(max_value)
                    best_origin = origin
                    best_size = (resized_width, resized_height)
                    best_distance = distance

            if (
                best_value < threshold
                or best_origin is None
                or best_size is None
                or best_distance > 0.20
            ):
                return None

            # Preserve the exact point within the matched element.
            hit_x = best_origin[0] + round(best_size[0] * rel_x)
            hit_y = best_origin[1] + round(best_size[1] * rel_y)

            touch_x = round(
                hit_x * max(1, self.maatouch.max_x) / max(1, shot_w)
            )
            touch_y = round(
                hit_y * max(1, self.maatouch.max_y) / max(1, shot_h)
            )
            touch_x = max(
                0, min(int(self.maatouch.max_x) - 1, touch_x)
            )
            touch_y = max(
                0, min(int(self.maatouch.max_y) - 1, touch_y)
            )
            return touch_x, touch_y

        except Exception as exc:
            print(
                f"[!] Visual target matching warning: {exc}",
                flush=True,
            )
            return None

    # -----------------------------------------------------------------
    # HIGH-FREQUENCY MAATOUCH TRAJECTORY INJECTION
    # -----------------------------------------------------------------

    def _send_maatouch_batch(
        self,
        commands: List[str],
    ) -> bool:
        """Send a scheduled command batch through the active MaaTouch pipe."""
        driver = self.maatouch
        sender = getattr(driver, "_send", None)
        process = getattr(driver, "proc", None)

        if not callable(sender) or process is None:
            return False

        try:
            poll = getattr(process, "poll", None)

            if callable(poll) and poll() is not None:
                return False

            sender("\n".join(commands) + "\n")
            return True

        except Exception as exc:
            print(
                f"[!] MaaTouch batch failed on {self.serial}: {exc}"
            )

            try:
                driver.touch_up(0)
            except Exception:
                pass

            return False

    def _inject_scaled_trajectory(
        self,
        start_x: int,
        start_y: int,
        trajectory: List[Dict[str, Any]],
        gesture_type: str = "SWIPE",
    ):
        """Generate a persona-conditioned QA gesture and send it via MaaTouch.

        Persona traits affect the bounded duration, path geometry, and device
        pressure value. Recorded start/end targets are retained. Drags keep
        their recorded intermediate geometry because a drag may operate a
        slider, draw, or reorder an object.
        """
        gesture_type = str(
            gesture_type or "SWIPE"
        ).upper()

        if not trajectory:
            return

        width = max(1, int(self.maatouch.max_x))
        height = max(1, int(self.maatouch.max_y))

        start_x = max(
            0,
            min(width, int(start_x)),
        )
        start_y = max(
            0,
            min(height, int(start_y)),
        )

        first = trajectory[0]
        last = trajectory[-1]

        first_ms = float(
            first.get("t_offset_ms", 0.0)
        )
        last_ms = float(
            last.get("t_offset_ms", first_ms + 1.0)
        )
        recorded_duration_ms = max(
            1.0,
            last_ms - first_ms,
        )

        source_resolution = self.data.get(
            "source_resolution",
            {},
        )

        source_width = max(
            1.0,
            float(source_resolution.get("width") or width),
        )
        source_height = max(
            1.0,
            float(source_resolution.get("height") or height),
        )

        if (
            all(key in first for key in ("x", "y"))
            and all(key in last for key in ("x", "y"))
        ):
            end_x = int(round(
                start_x
                + (
                    float(last["x"]) - float(first["x"])
                ) * width / source_width
            ))

            end_y = int(round(
                start_y
                + (
                    float(last["y"]) - float(first["y"])
                ) * height / source_height
            ))

        else:
            end_x = int(round(
                start_x
                + float(last.get("dx_norm", 0.0)) * width
            ))

            end_y = int(round(
                start_y
                + float(last.get("dy_norm", 0.0)) * height
            ))

        end_x = max(0, min(width, end_x))
        end_y = max(0, min(height, end_y))

        # One reproducible random stream per gesture.
        self._gesture_counter += 1
        gesture_seed = (
            int(self.persona.seed)
            + self._gesture_counter * 104729
        )

        rng = random.Random(gesture_seed)
        motion = self.persona.swipe_profile

        # Normalize persona-specific pressure to the MaaTouch pressure range.
        # This is an injected digitizer value, not calibrated physical force.
        max_pressure = max(
            1,
            int(getattr(self.maatouch, "max_pressure", 255)),
        )

        low_pressure, typical_pressure, high_pressure = (
            motion["pressure_ratio"]
        )

        pressure_ratio = rng.triangular(
            float(low_pressure),
            float(high_pressure),
            float(typical_pressure),
        )

        pressure_peak = max(
            1,
            min(
                max_pressure,
                int(round(max_pressure * pressure_ratio)),
            ),
        )

        pressure_ripple = float(motion["pressure_ripple"])
        pressure_envelope = float(motion["pressure_envelope"])
        pressure_state = 0.0

        def pressure_at(
            progress: float,
            update_state: bool = False,
        ) -> int:
            """Compute bounded pressure for the current contact."""
            nonlocal pressure_state

            u = max(
                0.0,
                min(1.0, float(progress)),
            )

            if update_state:
                pressure_state = (
                    0.64 * pressure_state
                    + rng.gauss(0.0, 0.46)
                )
                pressure_state = max(
                    -1.4,
                    min(1.4, pressure_state),
                )

            envelope = (
                math.sin(math.pi * u) ** 0.75
                if 0.0 < u < 1.0
                else 0.0
            )

            level = (
                1.0
                - pressure_envelope * (1.0 - envelope)
            )
            level *= 1.0 + pressure_ripple * pressure_state

            value = int(round(pressure_peak * level))

            return max(
                1,
                min(max_pressure, value),
            )

        initial_pressure = pressure_at(0.0)

        # Taps and long presses remain stationary.
        if gesture_type in ("TAP", "LONG_PRESS"):
            duration_scale = max(
                0.25,
                float(self.persona.dwell_scale),
            )

            duration_ms = max(
                1,
                int(
                    recorded_duration_ms
                    * duration_scale
                    * self.timing_scale
                ),
            )

            # Strictly cap TAP duration so it never triggers Android's Long Press (500ms bounds)
            if gesture_type == "TAP":
                duration_ms = min(duration_ms, 150)

            pressure = pressure_at(0.5)

            commands = [
                f"d 0 {start_x} {start_y} {pressure}",
                "c",
                f"w {duration_ms}",
                "u 0",
                "c",
            ]

            if self._send_maatouch_batch(commands):
                return

            self.maatouch.touch_down(
                0,
                start_x,
                start_y,
                pressure,
            )

            time.sleep(duration_ms / 1000.0)
            self.maatouch.touch_up(0)
            return

        # Apply the persona's movement vigor, then clamp it to its configured
        # duration range. A modest random factor varies consecutive gestures.
        vigor = max(
            0.55,
            float(self.persona.flick_vigor),
        )

        dwell_adjustment = (
            0.95
            + 0.05 * float(self.persona.dwell_scale)
        )

        duration_scale = (
            (1.0 / vigor)
            * dwell_adjustment
            * rng.triangular(0.94, 1.08, 1.0)
        )

        duration_min, duration_max = (
            motion["duration_scale_bounds"]
        )

        duration_scale = max(
            float(duration_min),
            min(float(duration_max), duration_scale),
        )

        duration_ms = max(
            35,
            int(round(
                recorded_duration_ms
                * duration_scale
                * self.timing_scale
            )),
        )

        duration_ms = min(2500, duration_ms)

        point_count = max(
            3,
            int(round(
                duration_ms * TARGET_TOUCH_RATE_HZ / 1000.0
            )) + 1,
        )
        point_count = min(120, point_count)

        delta_x = float(end_x - start_x)
        delta_y = float(end_y - start_y)
        distance = math.hypot(delta_x, delta_y)

        if distance > 1e-6:
            normal_x = -delta_y / distance
            normal_y = delta_x / distance
        else:
            normal_x = 0.0
            normal_y = 0.0

        moves = []

        if gesture_type == "DRAG":
            # Preserve the recorded drag geometry. Only duration and pressure
            # vary, so the test's intended slider/object destination is retained.
            for point in trajectory[1:]:
                raw_time = float(
                    point.get("t_offset_ms", first_ms)
                )

                time_progress = max(
                    0.0,
                    min(
                        1.0,
                        (raw_time - first_ms)
                        / recorded_duration_ms,
                    ),
                )

                elapsed_ms = int(round(
                    duration_ms * time_progress
                ))

                if (
                    all(key in point for key in ("x", "y"))
                    and all(key in first for key in ("x", "y"))
                ):
                    x = (
                        start_x
                        + (
                            float(point["x"])
                            - float(first["x"])
                        ) * width / source_width
                    )

                    y = (
                        start_y
                        + (
                            float(point["y"])
                            - float(first["y"])
                        ) * height / source_height
                    )

                else:
                    x = (
                        start_x
                        + float(point.get("dx_norm", 0.0)) * width
                    )

                    y = (
                        start_y
                        + float(point.get("dy_norm", 0.0)) * height
                    )

                x = max(0, min(width, int(round(x))))
                y = max(0, min(height, int(round(y))))

                pressure = pressure_at(
                    time_progress,
                    update_state=True,
                )

                moves.append((
                    elapsed_ms,
                    x,
                    y,
                    pressure,
                ))

            if (
                not moves
                or moves[-1][1:3] != (end_x, end_y)
            ):
                moves.append((
                    duration_ms,
                    end_x,
                    end_y,
                    pressure_at(1.0),
                ))
            else:
                moves[-1] = (
                    duration_ms,
                    end_x,
                    end_y,
                    pressure_at(1.0),
                )

        else:
            # ---------------------------------------------------------
            # PER-GESTURE SWIPE PATH
            # ---------------------------------------------------------
            ratio_low, ratio_typical, ratio_high = (
                motion["lateral_ratio"]
            )

            lateral_ratio = rng.triangular(
                float(ratio_low),
                float(ratio_high),
                float(ratio_typical),
            )

            shape_weights = motion["shape_weights"]
            shape_names = list(shape_weights)

            shape_mode = rng.choices(
                shape_names,
                weights=[
                    float(shape_weights[name])
                    for name in shape_names
                ],
                k=1,
            )[0]

            if shape_mode == "near_line":
                lateral_ratio *= rng.uniform(0.20, 0.48)

            elif shape_mode == "shallow_s":
                lateral_ratio *= rng.uniform(0.72, 1.00)

            elif shape_mode == "wander":
                lateral_ratio *= rng.uniform(0.82, 1.04)

            amplitude = min(
                distance * lateral_ratio,
                min(width, height) * 0.085,
            )

            preferred_direction = (
                -1.0
                if float(self.persona.curve_bias) < 0
                else 1.0
            )

            curve_direction = (
                preferred_direction
                if rng.random() < 0.55
                else -preferred_direction
            )

            # Generate a smooth, correlated path rather than independent
            # coordinate noise. The geometry changes each gesture.
            knot_count = rng.randint(4, 7)
            knot_values = [0.0]
            state = 0.0

            for knot_index in range(1, knot_count - 1):
                state = (
                    0.58 * state
                    + rng.gauss(0.0, 1.0)
                )

                knot_progress = (
                    knot_index / (knot_count - 1)
                )

                if shape_mode == "single_bend":
                    base = math.sin(
                        math.pi * knot_progress
                    )

                elif shape_mode == "shallow_s":
                    base = 0.62 * math.sin(
                        2.0 * math.pi * knot_progress
                    )

                elif shape_mode == "near_line":
                    base = 0.22 * math.sin(
                        math.pi * knot_progress
                    )

                else:
                    base = 0.0

                noise_strength = (
                    float(motion["roughness"]) * 0.42
                )

                knot_values.append(
                    curve_direction * base
                    + state * noise_strength
                )

            knot_values.append(0.0)

            peak = max(
                (abs(value) for value in knot_values),
                default=0.0,
            )

            if peak > 1e-9:
                knot_values = [
                    value / peak
                    for value in knot_values
                ]

            def path_offset(progress: float) -> float:
                u = max(
                    0.0,
                    min(1.0, float(progress)),
                )

                if (
                    u <= 0.0
                    or u >= 1.0
                    or amplitude <= 0.0
                ):
                    return 0.0

                scaled = u * (len(knot_values) - 1)
                left = min(
                    len(knot_values) - 2,
                    int(scaled),
                )
                local = scaled - left

                p0 = knot_values[max(0, left - 1)]
                p1 = knot_values[left]
                p2 = knot_values[left + 1]
                p3 = knot_values[
                    min(len(knot_values) - 1, left + 2)
                ]

                tangent1 = 0.5 * (p2 - p0)
                tangent2 = 0.5 * (p3 - p1)

                local2 = local * local
                local3 = local2 * local

                h00 = (
                    2.0 * local3
                    - 3.0 * local2
                    + 1.0
                )
                h10 = (
                    local3
                    - 2.0 * local2
                    + local
                )
                h01 = (
                    -2.0 * local3
                    + 3.0 * local2
                )
                h11 = local3 - local2

                value = (
                    h00 * p1
                    + h10 * tangent1
                    + h01 * p2
                    + h11 * tangent2
                )

                endpoint_fade = (
                    math.sin(math.pi * u) ** 0.75
                )

                return amplitude * value * endpoint_fade

            # Use a consistent acceleration character for each persona.
            # Path geometry remains independently sampled for each gesture.
            speed_curve = self.kinematic_law
            pressure_state = 0.0

            for index in range(1, point_count):
                time_progress = (
                    index / (point_count - 1)
                )

                path_progress = apply_kinematic_law(
                    time_progress,
                    speed_curve,
                )

                x = (
                    start_x
                    + delta_x * path_progress
                )

                y = (
                    start_y
                    + delta_y * path_progress
                )

                offset = path_offset(path_progress)

                x += normal_x * offset
                y += normal_y * offset

                if index == point_count - 1:
                    x = float(end_x)
                    y = float(end_y)

                x = max(
                    0,
                    min(width, int(round(x))),
                )
                y = max(
                    0,
                    min(height, int(round(y))),
                )

                elapsed_ms = int(round(
                    duration_ms * time_progress
                ))

                pressure = pressure_at(
                    time_progress,
                    update_state=True,
                )

                moves.append((
                    elapsed_ms,
                    x,
                    y,
                    pressure,
                ))

        # -------------------------------------------------------------
        # SEND THE COMPLETE GESTURE TO MAATOUCH
        # -------------------------------------------------------------
        commands = [
            f"d 0 {start_x} {start_y} {initial_pressure}",
            "c",
        ]

        previous_elapsed = 0

        for elapsed_ms, x, y, pressure in moves:
            wait_ms = max(
                0,
                int(elapsed_ms) - previous_elapsed,
            )

            if wait_ms:
                commands.append(f"w {wait_ms}")

            commands.extend([
                f"m 0 {x} {y} {pressure}",
                "c",
            ])

            previous_elapsed = int(elapsed_ms)

        commands.extend([
            "u 0",
            "c",
        ])

        if self._send_maatouch_batch(commands):
            return

        # Fallback for a driver without a usable MaaTouch pipe.
        self.maatouch.touch_down(
            0,
            start_x,
            start_y,
            initial_pressure,
        )

        started_at = time.perf_counter()

        for elapsed_ms, x, y, pressure in moves:
            remaining = (
                elapsed_ms / 1000.0
                - (time.perf_counter() - started_at)
            )

            if remaining > 0:
                time.sleep(remaining)

            self.maatouch.touch_move(
                0,
                x,
                y,
                pressure,
            )

        self.maatouch.touch_up(0)


# =====================================================================
# 3. PARALLEL MULTI-DEVICE FLEET RUNNER
# =====================================================================

def run_fleet_replay(
    recording_json_path: str, 
    target_serials: List[str], 
    persona_name: str = "random", 
    profile_name: str = "baseline",
    lockstep: bool = True,
    persona_obj=None
):
    rec_path = Path(recording_json_path)

    parsed_data = json.loads(
        rec_path.read_text(encoding="utf-8")
    )

    if not target_serials:
        raise ValueError(
            "Provide at least one target device serial."
        )

    if profile_name not in REPLAY_PROFILES:
        raise ValueError(
            f"Unknown timing profile: {profile_name}"
        )

    # Allow CLI to pass a seeded object, otherwise generate fresh from the API string
    persona = persona_obj or personas.generate_persona(archetype=persona_name)

    steps_value = parsed_data.get("steps")
    if not isinstance(steps_value, list) or not steps_value:
        raise ValueError(
            f"Recording {rec_path} contains no replayable steps."
        )
    malformed = [
        index + 1 for index, item in enumerate(steps_value)
        if not isinstance(item, dict) or not item.get("event_type")
    ]
    if malformed:
        raise ValueError(
            "Recording contains steps without a valid event_type at indexes: "
            + ", ".join(map(str, malformed[:20]))
        )

    number_of_devices = len(target_serials)
    number_of_steps = len(steps_value)

    print(
        f"[*] Replay engine build: {REPLAY_ENGINE_BUILD_ID} | source={Path(__file__).resolve()}",
        flush=True,
    )

    print(
        "[*] Starting synchronized parallel replay of "
        f"{rec_path.name} across {number_of_devices} devices..."
    )

    print(
        f"[*] Persona={persona.name} ({persona.archetype}), "
        f"seed={persona.seed}, "
        f"law={KINEMATIC_LAWS.get(persona.archetype, 'linear')}, "
        f"text={persona.wpm:.1f} WPM, "
        f"timing={profile_name}."
    )

    start_barrier = threading.Barrier(number_of_devices)

    lockstep_barriers = (
        [
            threading.Barrier(number_of_devices)
            for _ in range(number_of_steps)
        ]
        if lockstep and number_of_devices > 1
        else None
    )

    workers = [
        DeviceReplayWorker(
            serial=serial,
            recording_path=rec_path,
            parsed_data=parsed_data,
            start_barrier=start_barrier,
            lockstep_barriers=lockstep_barriers,
            persona=persona,
            profile_name=profile_name,
        )
        for serial in target_serials
    ]

    fleet_errors = []
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=number_of_devices
    ) as pool:
        futures = {
            pool.submit(worker.execute_test): worker.serial
            for worker in workers
        }

        for future in concurrent.futures.as_completed(futures):
            serial = futures[future]

            try:
                report = future.result()
                passed = int(report.get("completed_steps", 0) or 0)
                total = int(report.get("total_steps", number_of_steps) or 0)
                skipped = int(report.get("skipped_steps", 0) or 0)
                attempted = int(report.get("attempted_steps", 0) or 0)
                failed = int(report.get("failed_steps", 0) or 0)
                not_run = int(report.get("remaining_steps_not_run", max(0, total - attempted)) or 0)
                if total != number_of_steps:
                    fleet_errors.append(
                        f"{serial}: report total {total} does not match recording total {number_of_steps}"
                    )
                if passed != total or skipped or failed or not_run or attempted != total:
                    fleet_errors.append(
                        f"{serial}: {passed}/{total} passed; attempted={attempted}, "
                        f"skipped={skipped}, failed={failed}, not_run={not_run}"
                    )

                print(f"\n=== Replay Report [{serial}] ===")
                print(f"[*] Persona: {report.get('persona', 'Unknown')} | Text Rate: {report.get('text_input_wpm', 0)} WPM | Vigor: {report.get('flick_vigor', 0)}x")
                print(
                    f"[*] Status: {report.get('completed_steps', 0)} / "
                    f"{report.get('total_steps', 0)} steps passed "
                    f"({report.get('skipped_steps', 0)} skipped, "
                    f"{report.get('attempted_steps', 0)} attempted, "
                    f"{report.get('failed_steps', 0)} failed)."
                )
                if report.get("stopped_at_step") is not None:
                    print(
                        f"[!] Replay stopped at step {report['stopped_at_step']}; "
                        f"{report.get('remaining_steps_not_run', 0)} later steps were not run.\n"
                    )
                else:
                    print()

                for step in report.get("steps", []):
                    s_id = f"{step.get('step_id', 0):02d}"
                    s_type = step.get("type", "UNKNOWN")
                    status = step.get("status", "FAILED")
                    
                    if status == "PASSED":
                        mark = "[+]"
                        if s_type == "TEXT_INPUT" and "text_input" in step:
                            ti = step["text_input"]
                            details = f"({ti.get('characters', 0)} chars physically typed @ {ti.get('wpm_achieved', 0.0)} WPM)"
                        else:
                            details = f"(Target matched via {step.get('match_tier', 'N/A')})"
                    elif str(status).startswith("SKIPPED"):
                        mark = "[*]"
                        details = f"(Skipped: {step.get('reason', status)})"
                    else:
                        mark = "[!]"
                        details = f"- FAILED: {step.get('reason', 'Unknown error')}"
                        
                    print(f"    {mark} Step {s_id} | {s_type:<10} {details}")
                print("\n======================================\n")

            except Exception as exc:
                fleet_errors.append(
                    f"{serial}: worker crashed with {type(exc).__name__}: {exc}"
                )
                print(
                    f"[!] Device {serial} crashed during replay: "
                    f"{type(exc).__name__}: {exc}",
                    flush=True,
                )

    if fleet_errors:
        details = " | ".join(fleet_errors)
        print(
            f"[!] FLEET REPLAY FAILED: {details}",
            flush=True,
        )
        raise RuntimeError(details)

    print(
        f"[+] FLEET REPLAY PASSED: all {number_of_steps} steps passed "
        f"on {number_of_devices} device(s).",
        flush=True,
    )


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(
        description=(
            "Replay an Android recording using "
            "a selectable QA persona."
        )
    )

    parser.add_argument(
        "recording_json",
        help="Path to recording.json",
    )

    parser.add_argument(
        "serials",
        nargs="+",
        help="Target ADB serials",
    )

    parser.add_argument(
        "--persona",
        choices=[
            *personas.ARCHETYPE_NAMES,
            "random",
        ],
        default=None,
        help="Omit to select interactively.",
    )

    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Optional seed for reproducible persona traits.",
    )

    parser.add_argument(
        "--profile",
        choices=tuple(REPLAY_PROFILES),
        default="baseline",
        help="Overall QA timing profile.",
    )
    
    parser.add_argument(
        "--no-lockstep", 
        action="store_true", 
        help="Disable lockstep synchronization"
    )

    args = parser.parse_args()

    try:
        archetype = choose_persona(args.persona)

        selected_persona = make_persona(
            archetype,
            args.seed,
        )

        run_fleet_replay(
            recording_json_path=args.recording_json,
            target_serials=args.serials,
            persona_name=archetype,
            profile_name=args.profile,
            lockstep=not args.no_lockstep,
            persona_obj=selected_persona
        )

    except RuntimeError as exc:
        print(f"[!] Replay failed; one or more steps/devices did not complete: {exc}", flush=True)
        sys.exit(3)
    except (
        ValueError,
        OSError,
        json.JSONDecodeError,
    ) as exc:
        print(f"[!] Replay could not start: {exc}", flush=True)
        sys.exit(2)
