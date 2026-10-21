import argparse
import concurrent.futures
import json
import math
import random
import sys
import time
import threading
import re
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


def dismiss_oem_system_dialogs(
    u2_dev: u2.Device,
) -> bool:
    """Try to dismiss recognized system permission/security dialogs."""
    try:
        current_package = (
            u2_dev.app_current().get("package", "")
        )

        if (
            "permissioncontroller" in current_package
            or "securitycenter" in current_package
            or "packageinstaller" in current_package
        ):
            for label in OEM_ALLOW_PATTERNS:
                button = u2_dev(textContains=label)

                if button.exists:
                    button.click()
                    time.sleep(0.4)
                    return True

    except Exception:
        pass

    return False

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
            if not found: continue

            l, t, r, b = node["bounds"]
            w, h = r - l, b - t
            if w > node["screen_w"] * (0.80 if semantic == "space" else 0.28) or h > 0.16 * node["screen_h"]: continue
            matches.append(node)

        if not matches and semantic in ("sym_switch", "alpha_switch") and nodes:
            bl_cands = [n for n in nodes if n["bounds"][0] < n["screen_w"]*0.22 and (n["bounds"][1]+n["bounds"][3])/2 > n["screen_h"]*0.84]
            if bl_cands:
                bl_cands.sort(key=lambda n: (-n["bounds"][3], n["bounds"][0]))
                return bl_cands[0]["bounds"], bl_cands[0]["attrs"]

        if not matches:
            raise RuntimeError(f"Key '{key_name}' not found on layer '{self._current_kb_layer}'.")
        matches.sort(key=lambda n: (n["area"], n["clickable_rank"]))
        return matches[0]["bounds"], matches[0]["attrs"]

    def _tap_android_keyboard_key(self, key_name: str):
        bounds, attrs = self._find_keyboard_key(key_name)
        l, t, r, b = bounds
        x, y = int((l + r) / 2), int((t + b) / 2)
        pressure = max(1, min(int(self.maatouch.max_pressure), 75))
        # Ensure we write a clean, atomic MaaTouch command batch so it never hangs
        self.maatouch._send(f"d 0 {x} {y} {pressure}\nc\nw 22\nu 0\nc\n")
        time.sleep(0.025)
        return attrs

    def _switch_keyboard_layer(self, target_layer: str):
        if self._current_kb_layer == target_layer: return
        if target_layer == "alpha":
            self._tap_android_keyboard_key("alpha_switch")
            self._current_kb_layer = "alpha"
        elif target_layer == "sym1":
            if self._current_kb_layer == "alpha": self._tap_android_keyboard_key("sym_switch")
            elif self._current_kb_layer == "sym2": self._tap_android_keyboard_key("sym_switch")
            self._current_kb_layer = "sym1"
        elif target_layer == "sym2":
            if self._current_kb_layer == "alpha":
                self._tap_android_keyboard_key("sym_switch")
                time.sleep(0.14)
            self._tap_android_keyboard_key("more_sym_switch")
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

    def execute_test(self) -> Dict[str, Any]:
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
                        pass

                event_type = step["event_type"]

                if (
                    event_type == "SWIPE"
                    and index > 0
                    and steps[index - 1]["event_type"] == "SWIPE"
                ):
                    next_tap = self._peek_next_tap_step(
                        steps,
                        index,
                    )

                    if (
                        next_tap
                        and self._is_anchor_currently_visible(
                            next_tap.get("anchor", {})
                        )
                    ):
                        results.append({
                            "step_id": step["step_id"],
                            "type": "SWIPE",
                            "status": "SKIPPED_TARGET_ALREADY_VISIBLE",
                        })
                        continue

                step_result = self._run_step(
                    step,
                    last_swipe_trajectory,
                )
                results.append(step_result)

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
            self.maatouch.stop()

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
            "completed_steps": len(results),
            "total_steps": self.data.get(
                "total_steps",
                len(steps),
            ),
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

        return (
            self._find_bounds_in_hierarchy(
                anchor,
                (0.5, 0.5),
            )
            is not None
        )

    def _run_step(
        self,
        step: Dict[str, Any],
        prev_swipe_trajectory: Optional[
            List[Dict[str, Any]]
        ] = None,
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
            self.maatouch.press_key(int(step["keycode"]))

            return {
                "step_id": step_id,
                "type": event_type,
                "status": "PASSED",
            }

        if event_type == "TEXT_INPUT":
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

                return {
                    "step_id": step_id,
                    "type": event_type,
                    "status": "PASSED",
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
            width = self.maatouch.max_x
            height = self.maatouch.max_y

            first = trajectory[0]
            start_x = int(first["rx"] * width)
            start_y = int(first["ry"] * height)

            self._inject_scaled_trajectory(
                start_x,
                start_y,
                trajectory,
                gesture_type=event_type,
            )

            time.sleep(0.55)

            return {
                "step_id": step_id,
                "type": "SWIPE",
                "status": "PASSED",
                "match_tier": "SCREEN_NORMALIZED_SWIPE",
                "injected_start": (start_x, start_y),
            }

        anchor = step.get("anchor") or {}

        resolved_xy, match_tier = self._wait_and_resolve_target(
            anchor,
            trajectory[0],
            timeout_s=6.0,
            scroll_recovery_trajectory=prev_swipe_trajectory,
        )

        if resolved_xy is None:
            return {
                "step_id": step_id,
                "type": event_type,
                "status": "FAILED",
                "reason": (
                    f"Could not resolve anchor: "
                    f"text='{anchor.get('text')}' "
                    f"desc='{anchor.get('content_desc')}' "
                    f"id='{anchor.get('resource_id')}'"
                ),
            }

        start_x, start_y = resolved_xy

        self._inject_scaled_trajectory(
            start_x,
            start_y,
            trajectory,
            gesture_type=event_type,
        )

        return {
            "step_id": step_id,
            "type": event_type,
            "status": "PASSED",
            "match_tier": match_tier,
            "injected_start": (start_x, start_y),
        }

    # -----------------------------------------------------------------
    # 3-TIER TARGET RESOLUTION + AUTO-SCROLL RECOVERY
    # -----------------------------------------------------------------

    def _wait_and_resolve_target(
        self,
        anchor: Dict[str, Any],
        first_sample: Dict[str, Any],
        timeout_s: float = 6.0,
        scroll_recovery_trajectory: Optional[
            List[Dict[str, Any]]
        ] = None,
    ) -> Tuple[Optional[Tuple[int, int]], str]:
        deadline = time.perf_counter() + timeout_s

        has_semantic_id = bool(
            anchor.get("resource_id")
            or anchor.get("text")
            or anchor.get("content_desc")
        )

        expected_norm_xy = (
            first_sample.get("rx", 0.5),
            first_sample.get("ry", 0.5),
        )

        # Give it a generous buffer of extra scrolls to search deep app drawers
        extra_scrolls_left = 7 if scroll_recovery_trajectory else 0
        screen_start_time = time.perf_counter()

        while time.perf_counter() < deadline:
            dismiss_oem_system_dialogs(self.u2_dev)

            if has_semantic_id:
                bounds = self._find_bounds_in_hierarchy(anchor, expected_norm_xy)
                if bounds:
                    left, top, right, bottom = bounds
                    rx = anchor.get("element_rx", 0.5)
                    ry = anchor.get("element_ry", 0.5)
                    hit_x = int(left + rx * max(1, right - left))
                    hit_y = int(top + ry * max(1, bottom - top))
                    return (hit_x, hit_y), "TIER1_UI_ANCHOR"

            crop_relative = anchor.get("visual_crop_path")
            if crop_relative:
                crop_full = self.recording_dir / crop_relative
                if crop_full.exists():
                    visual_hit = self._match_template_multiscale(crop_full)
                    if visual_hit:
                        return visual_hit, "TIER2_VISUAL_TEMPLATE"

            # SMART SCROLL RECOVERY: Wait 1.2s for UI to settle before scrolling
            time_on_screen = time.perf_counter() - screen_start_time
            if time_on_screen > 1.2 and extra_scrolls_left > 0 and scroll_recovery_trajectory:
                extra_scrolls_left -= 1
                width = self.maatouch.max_x
                height = self.maatouch.max_y
                first_point = scroll_recovery_trajectory[0]

                print(f"[*] Target not found, triggering auto-scroll recovery ({extra_scrolls_left} left)...")
                self._inject_scaled_trajectory(
                    int(first_point["rx"] * width),
                    int(first_point["ry"] * height),
                    scroll_recovery_trajectory,
                    gesture_type="SWIPE",
                )

                # Reset the screen timer and extend the total deadline so it has time to look
                screen_start_time = time.perf_counter()
                deadline += 2.0
                time.sleep(0.8) # Allow scroll momentum to settle
                continue

            if not has_semantic_id:
                break

            time.sleep(0.4)

        if anchor.get("text") or anchor.get("content_desc"):
            return None, "NOT_FOUND"

        fallback_x = int(first_sample["rx"] * self.maatouch.max_x)
        fallback_y = int(first_sample["ry"] * self.maatouch.max_y)

        return (fallback_x, fallback_y), "TIER3_NORMALIZED_XY"

    def _find_bounds_in_hierarchy(
        self,
        anchor: Dict[str, Any],
        expected_norm_xy: Tuple[float, float],
    ) -> Optional[Tuple[int, int, int, int]]:
        try:
            xml_string = self.u2_dev.dump_hierarchy(
                compressed=False
            )
            root = ET.fromstring(xml_string.encode("utf-8"))

        except Exception:
            return None

        target_rid = anchor.get("resource_id", "").strip()
        target_text = anchor.get("text", "").strip()
        target_desc = anchor.get("content_desc", "").strip()
        target_class = anchor.get("class_name", "").strip()

        screen_width = max(1, self.maatouch.max_x)
        screen_height = max(1, self.maatouch.max_y)

        expected_x, expected_y = expected_norm_xy

        best_bounds = None
        best_score = -1.0

        for element in root.iter("node"):
            resource_id = element.attrib.get(
                "resource-id",
                "",
            ).strip()

            text = element.attrib.get("text", "").strip()
            description = element.attrib.get(
                "content-desc",
                "",
            ).strip()

            class_name = element.attrib.get(
                "class",
                "",
            ).strip()

            if (
                target_text
                and text.lower() != target_text.lower()
                and description.lower() != target_text.lower()
            ):
                continue

            if (
                target_desc
                and description.lower() != target_desc.lower()
                and text.lower() != target_desc.lower()
            ):
                continue

            score = 0.0

            if target_text and (
                text.lower() == target_text.lower()
                or description.lower() == target_text.lower()
            ):
                score += 10.0

            if target_desc and (
                description.lower() == target_desc.lower()
                or text.lower() == target_desc.lower()
            ):
                score += 10.0

            if target_rid and resource_id == target_rid:
                score += 5.0

            elif (
                target_rid
                and resource_id.split("/")[-1]
                == target_rid.split("/")[-1]
            ):
                score += 4.0

            if target_class and class_name == target_class:
                score += 1.0

            if score < 4.0:
                continue

            bounds_string = element.attrib.get("bounds", "")

            if not bounds_string:
                continue

            parts = (
                bounds_string
                .replace("][", ",")
                .strip("[]")
                .split(",")
            )

            try:
                left, top, right, bottom = map(int, parts)
            except ValueError:
                continue

            if right <= left or bottom <= top:
                continue

            center_x_norm = (
                (left + right) * 0.5
            ) / screen_width

            center_y_norm = (
                (top + bottom) * 0.5
            ) / screen_height

            distance = math.hypot(
                center_x_norm - expected_x,
                center_y_norm - expected_y,
            )

            proximity_bonus = max(0.0, 1.0 - distance)
            total_score = score + proximity_bonus

            if total_score > best_score:
                best_score = total_score
                best_bounds = (
                    left,
                    top,
                    right,
                    bottom,
                )

        return best_bounds

    def _match_template_multiscale(
        self,
        crop_path: Path,
        threshold: float = 0.80,
    ) -> Optional[Tuple[int, int]]:
        try:
            screen_bgr = self.u2_dev.screenshot(
                format="opencv"
            )
            template = cv2.imread(str(crop_path))

            if screen_bgr is None or template is None:
                return None

            gray_screen = cv2.cvtColor(
                screen_bgr,
                cv2.COLOR_BGR2GRAY,
            )

            gray_template = cv2.cvtColor(
                template,
                cv2.COLOR_BGR2GRAY,
            )

            template_height, template_width = (
                gray_template.shape[:2]
            )

            best_value = -1.0
            best_center = None

            for scale in np.linspace(0.8, 1.2, 5):
                resized_width = int(template_width * scale)
                resized_height = int(template_height * scale)

                if (
                    resized_width >= gray_screen.shape[1]
                    or resized_height >= gray_screen.shape[0]
                    or resized_width < 12
                    or resized_height < 12
                ):
                    continue

                resized = cv2.resize(
                    gray_template,
                    (resized_width, resized_height),
                )

                result = cv2.matchTemplate(
                    gray_screen,
                    resized,
                    cv2.TM_CCOEFF_NORMED,
                )

                _, max_value, _, max_location = (
                    cv2.minMaxLoc(result)
                )

                if max_value > best_value:
                    best_value = max_value
                    best_center = (
                        max_location[0] + resized_width // 2,
                        max_location[1] + resized_height // 2,
                    )

            if best_value >= threshold and best_center:
                return best_center

        except Exception:
            pass

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

    number_of_devices = len(target_serials)
    number_of_steps = len(parsed_data.get("steps", []))

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

                print(
                    f"\n=== Replay Report [{serial}] ==="
                )

                print(json.dumps(report, indent=2))

            except Exception as exc:
                print(
                    f"[!] Device {serial} crashed during replay: "
                    f"{exc}"
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

    except (
        ValueError,
        OSError,
        json.JSONDecodeError,
    ) as exc:
        print(f"[!] Replay could not start: {exc}")
        sys.exit(2)