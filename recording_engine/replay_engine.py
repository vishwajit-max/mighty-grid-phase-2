import concurrent.futures
import json
import math
import sys
import time
from pathlib import Path
from typing import Dict, Any, Optional, Tuple, List

import cv2
import lxml.etree as ET
import numpy as np
import uiautomator2 as u2
import threading

from recorder_engine import MaaTouchDriver


# =====================================================================
# 1. OEM POPUP & PERMISSION WATCHER (Realme UI / One UI / HyperOS)
# =====================================================================

OEM_ALLOW_PATTERNS = [
    "While using the app", "Only this time", "Allow", "ALLOW",
    "Agree", "Continue", "Got it", "Install anyway"
]


def dismiss_oem_system_dialogs(u2_dev: u2.Device) -> bool:
    """
    Automatically clicks Allow on system permission controllers or OEM security
    dialogs if they intercept the foreground during replay.
    """
    try:
        curr = u2_dev.app_current().get("package", "")
        if (
            "permissioncontroller" in curr
            or "securitycenter" in curr
            or "packageinstaller" in curr
        ):
            for label in OEM_ALLOW_PATTERNS:
                btn = u2_dev(textContains=label)
                if btn.exists:
                    btn.click()
                    time.sleep(0.4)
                    return True
    except Exception:
        pass
    return False


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
        lockstep_barriers: Optional[List[threading.Barrier]] = None
    ):
        # Keep __init__ 100% lightweight (0ms blocking I/O on main thread)
        self.serial = serial
        self.recording_dir = recording_path.parent
        self.data = parsed_data
        self.start_barrier = start_barrier
        self.lockstep_barriers = lockstep_barriers
        self.u2_dev: Optional[u2.Device] = None
        self.maatouch: Optional[MaaTouchDriver] = None

    def execute_test(self) -> Dict[str, Any]:
        # 1. Connect uiautomator2 and MaaTouch in parallel
        self.u2_dev = u2.connect(self.serial)
        self.maatouch = MaaTouchDriver(self.serial)
        self.maatouch.start()

        # 2. Force the device to the correct starting screen (Home or Target App)
        self._prepare_initial_state()

        # 3. Wait at the starting line until ALL devices are connected and reset
        if self.start_barrier is not None:
            try:
                self.start_barrier.wait(timeout=15.0)
            except threading.BrokenBarrierError:
                print(f"[!] Start barrier timeout on {self.serial}, proceeding...")

        results = []
        steps = self.data["steps"]
        last_swipe_trajectory: Optional[List[Dict[str, Any]]] = None

        try:
            for idx, step in enumerate(steps):
                # Optional lockstep sync at the start of each step
                if self.lockstep_barriers and idx < len(self.lockstep_barriers):
                    try:
                        self.lockstep_barriers[idx].wait(timeout=12.0)
                    except threading.BrokenBarrierError:
                        pass

                ev_type = step["event_type"]

                if ev_type == "SWIPE" and idx > 0 and steps[idx - 1]["event_type"] == "SWIPE":
                    next_tap = self._peek_next_tap_step(steps, idx)
                    if next_tap and self._is_anchor_currently_visible(next_tap.get("anchor", {})):
                        results.append({
                            "step_id": step["step_id"],
                            "type": "SWIPE",
                            "status": "SKIPPED_TARGET_ALREADY_VISIBLE"
                        })
                        continue

                step_res = self._run_step(step, last_swipe_trajectory)
                results.append(step_res)

                if ev_type == "SWIPE" and step.get("trajectory"):
                    last_swipe_trajectory = step["trajectory"]
                elif ev_type != "SWIPE":
                    last_swipe_trajectory = None

                if step_res["status"] == "FAILED":
                    break
        finally:
            self.maatouch.stop()

        return {
            "serial": self.serial,
            "completed_steps": len(results),
            "total_steps": self.data["total_steps"],
            "steps": results
        }

    def _prepare_initial_state(self):
        """Wakes device and resets to Home screen or target app before Step 1."""
        print(f"[*] Resetting initial state for {self.serial}...")
        try:
            self.u2_dev.screen_on()
            steps = self.data.get("steps", [])
            if not steps:
                return

            first_pkg = steps[0].get("pre_package", "")
            
            # Identify OEM launchers (Realme, Poco, Samsung, Pixel)
            launcher_keywords = ["launcher", "miui.home", "sec.android.app", "nexuslauncher"]
            is_home = not first_pkg or any(k in first_pkg.lower() for k in launcher_keywords)
            
            if is_home:
                # Press HOME twice to exit apps and reset to the default launcher page
                self.u2_dev.press("home")
                time.sleep(0.5)
                self.u2_dev.press("home")
                time.sleep(1.0)
            else:
                # If the recording started inside a specific app, launch it cleanly
                self.u2_dev.app_start(first_pkg, stop=False)
                time.sleep(2.0)
        except Exception as e:
            print(f"[!] Pre-flight state warning on {self.serial}: {e}")

    def _peek_next_tap_step(self, steps: List[Dict[str, Any]], start_idx: int) -> Optional[Dict[str, Any]]:
        """Finds the first TAP/LONG_PRESS step immediately following a chain of SWIPEs."""
        for s in steps[start_idx + 1:]:
            if s["event_type"] == "SWIPE":
                continue
            if s["event_type"] in ("TAP", "LONG_PRESS"):
                return s
            break
        return None

    def _is_anchor_currently_visible(self, anchor: Dict[str, Any]) -> bool:
        if not anchor:
            return False
        if not (anchor.get("text") or anchor.get("content_desc") or anchor.get("resource_id")):
            return False
        return self._find_bounds_in_hierarchy(anchor, (0.5, 0.5)) is not None

    def _run_step(
        self,
        step: Dict[str, Any],
        prev_swipe_trajectory: Optional[List[Dict[str, Any]]] = None
    ) -> Dict[str, Any]:
        step_id = step["step_id"]
        ev_type = step["event_type"]

        # Respect human think-time (capped at 2.0s)
        think_s = min(step.get("think_time_ms", 300) / 1000.0, 2.0)
        if think_s > 0.05:
            time.sleep(think_s)

        # 1. Hardware keys & Semantic Text
        if ev_type == "KEY_EVENT":
            self.maatouch.press_key(int(step["keycode"]))
            return {"step_id": step_id, "type": ev_type, "status": "PASSED"}

        if ev_type == "TEXT_INPUT":
            try:
                self.u2_dev.send_keys(step["text_payload"], clear=False)
            except Exception:
                self.maatouch.input_text(step["text_payload"])
            return {"step_id": step_id, "type": ev_type, "status": "PASSED"}

        trajectory = step.get("trajectory") or []
        if not trajectory:
            return {"step_id": step_id, "type": ev_type, "status": "SKIPPED"}

        # 2. SWIPE: Always start from screen-normalized coordinates so swipes never
        # snap to the top-left icon in an app drawer or RecyclerView.
        if ev_type == "SWIPE":
            w, h = self.maatouch.max_x, self.maatouch.max_y
            first = trajectory[0]
            start_x = int(first["rx"] * w)
            start_y = int(first["ry"] * h)
            self._inject_scaled_trajectory(start_x, start_y, trajectory)
            # Let fling momentum settle before the next step dumps the UI hierarchy
            time.sleep(0.55)
            return {
                "step_id": step_id,
                "type": "SWIPE",
                "status": "PASSED",
                "match_tier": "SCREEN_NORMALIZED_SWIPE",
                "injected_start": (start_x, start_y)
            }

        # 3. TAP / LONG_PRESS: Resolve semantic anchor (with auto-scroll recovery if preceded by SWIPE)
        anchor = step.get("anchor") or {}
        resolved_xy, match_tier = self._wait_and_resolve_target(
            anchor,
            trajectory[0],
            timeout_s=6.0,
            scroll_recovery_trajectory=prev_swipe_trajectory
        )
        if resolved_xy is None:
            return {
                "step_id": step_id,
                "type": ev_type,
                "status": "FAILED",
                "reason": f"Could not resolve anchor: text='{anchor.get('text')}' desc='{anchor.get('content_desc')}' id='{anchor.get('resource_id')}'"
            }

        start_x, start_y = resolved_xy
        self._inject_scaled_trajectory(start_x, start_y, trajectory)
        return {
            "step_id": step_id,
            "type": ev_type,
            "status": "PASSED",
            "match_tier": match_tier,
            "injected_start": (start_x, start_y)
        }

    # -----------------------------------------------------------------
    # 3-TIER TARGET RESOLUTION + AUTO-SCROLL RECOVERY
    # -----------------------------------------------------------------

    def _wait_and_resolve_target(
        self,
        anchor: Dict[str, Any],
        first_sample: Dict[str, Any],
        timeout_s: float = 6.0,
        scroll_recovery_trajectory: Optional[List[Dict[str, Any]]] = None
    ) -> Tuple[Optional[Tuple[int, int]], str]:
        deadline = time.perf_counter() + timeout_s
        has_semantic_id = bool(
            anchor.get("resource_id") or anchor.get("text") or anchor.get("content_desc")
        )
        expected_norm_xy = (first_sample.get("rx", 0.5), first_sample.get("ry", 0.5))
        extra_scrolls_left = 5 if scroll_recovery_trajectory else 0

        while time.perf_counter() < deadline:
            dismiss_oem_system_dialogs(self.u2_dev)

            # TIER 1: Match UI Hierarchy Node
            if has_semantic_id:
                bounds = self._find_bounds_in_hierarchy(anchor, expected_norm_xy)
                if bounds:
                    l, t, r, b = bounds
                    rx = anchor.get("element_rx", 0.5)
                    ry = anchor.get("element_ry", 0.5)
                    hit_x = int(l + rx * max(1, (r - l)))
                    hit_y = int(t + ry * max(1, (b - t)))
                    return (hit_x, hit_y), "TIER1_UI_ANCHOR"

            # TIER 2: Multi-Scale Visual Crop Match
            crop_rel = anchor.get("visual_crop_path")
            if crop_rel:
                crop_full = self.recording_dir / crop_rel
                if crop_full.exists():
                    vis_hit = self._match_template_multiscale(crop_full)
                    if vis_hit:
                        return vis_hit, "TIER2_VISUAL_TEMPLATE"

            # If preceded by a SWIPE and target still isn't visible, repeat the scroll swipe
            if extra_scrolls_left > 0 and scroll_recovery_trajectory:
                extra_scrolls_left -= 1
                w, h = self.maatouch.max_x, self.maatouch.max_y
                s0 = scroll_recovery_trajectory[0]
                self._inject_scaled_trajectory(
                    int(s0["rx"] * w), int(s0["ry"] * h), scroll_recovery_trajectory
                )
                deadline += 1.4
                time.sleep(0.55)
                continue

            if not has_semantic_id:
                break

            time.sleep(0.4)

        # If a specific label (like "Play Store") was required and never found, fail cleanly
        if anchor.get("text") or anchor.get("content_desc"):
            return None, "NOT_FOUND"

        # TIER 3: Screen-Normalized Coordinate Fallback (for unlabeled views)
        w, h = self.maatouch.max_x, self.maatouch.max_y
        fallback_x = int(first_sample["rx"] * w)
        fallback_y = int(first_sample["ry"] * h)
        return (fallback_x, fallback_y), "TIER3_NORMALIZED_XY"

    def _find_bounds_in_hierarchy(
        self,
        anchor: Dict[str, Any],
        expected_norm_xy: Tuple[float, float]
    ) -> Optional[Tuple[int, int, int, int]]:
        try:
            xml_str = self.u2_dev.dump_hierarchy(compressed=False)
            root = ET.fromstring(xml_str.encode("utf-8"))
        except Exception:
            return None

        target_rid = anchor.get("resource_id", "").strip()
        target_txt = anchor.get("text", "").strip()
        target_desc = anchor.get("content_desc", "").strip()
        target_cls = anchor.get("class_name", "").strip()

        w_screen = max(1, self.maatouch.max_x)
        h_screen = max(1, self.maatouch.max_y)
        exp_rx, exp_ry = expected_norm_xy

        best_bounds = None
        best_score = -1.0

        for elem in root.iter("node"):
            rid = elem.attrib.get("resource-id", "").strip()
            txt = elem.attrib.get("text", "").strip()
            desc = elem.attrib.get("content-desc", "").strip()
            cls = elem.attrib.get("class", "").strip()

            # Hard Guard: If the recorded element had text or content-desc (e.g., "Play Store"),
            # do not match another icon just because it shares the generic launcher resource-id!
            if target_txt and txt.lower() != target_txt.lower() and desc.lower() != target_txt.lower():
                continue
            if target_desc and desc.lower() != target_desc.lower() and txt.lower() != target_desc.lower():
                continue

            score = 0.0
            if target_txt and (txt.lower() == target_txt.lower() or desc.lower() == target_txt.lower()):
                score += 10.0
            if target_desc and (desc.lower() == target_desc.lower() or txt.lower() == target_desc.lower()):
                score += 10.0

            if target_rid and rid == target_rid:
                score += 5.0
            elif target_rid and rid.split("/")[-1] == target_rid.split("/")[-1]:
                score += 4.0

            if target_cls and cls == target_cls:
                score += 1.0

            if score < 4.0:
                continue

            bounds_str = elem.attrib.get("bounds", "")
            if not bounds_str:
                continue
            parts = bounds_str.replace("][", ",").strip("[]").split(",")
            l, t, r, b = map(int, parts)
            if r <= l or b <= t:
                continue

            # Tie-breaker: Spatial proximity to recorded normalized (rx, ry)
            cx_norm = ((l + r) * 0.5) / w_screen
            cy_norm = ((t + b) * 0.5) / h_screen
            dist = math.hypot(cx_norm - exp_rx, cy_norm - exp_ry)
            proximity_bonus = max(0.0, 1.0 - dist)  # 0.0 to 1.0

            total_score = score + proximity_bonus
            if total_score > best_score:
                best_score = total_score
                best_bounds = (l, t, r, b)

        return best_bounds

    def _match_template_multiscale(self, crop_path: Path, threshold: float = 0.80) -> Optional[Tuple[int, int]]:
        try:
            screen_bgr = self.u2_dev.screenshot(format="opencv")
            template = cv2.imread(str(crop_path))
            if screen_bgr is None or template is None:
                return None

            gray_screen = cv2.cvtColor(screen_bgr, cv2.COLOR_BGR2GRAY)
            gray_tmpl = cv2.cvtColor(template, cv2.COLOR_BGR2GRAY)
            th, tw = gray_tmpl.shape[:2]

            best_val = -1.0
            best_center = None

            for scale in np.linspace(0.8, 1.2, 5):
                rw, rh = int(tw * scale), int(th * scale)
                if rw >= gray_screen.shape[1] or rh >= gray_screen.shape[0] or rw < 12 or rh < 12:
                    continue
                resized = cv2.resize(gray_tmpl, (rw, rh))
                res = cv2.matchTemplate(gray_screen, resized, cv2.TM_CCOEFF_NORMED)
                _, max_val, _, max_loc = cv2.minMaxLoc(res)
                if max_val > best_val:
                    best_val = max_val
                    best_center = (max_loc[0] + rw // 2, max_loc[1] + rh // 2)

            if best_val >= threshold and best_center:
                return best_center
        except Exception:
            pass
        return None

    # -----------------------------------------------------------------
    # HIGH-FREQUENCY MAATOUCH TRAJECTORY INJECTION
    # -----------------------------------------------------------------

    def _inject_scaled_trajectory(self, start_x: int, start_y: int, trajectory: List[Dict[str, Any]]):
        w, h = self.maatouch.max_x, self.maatouch.max_y
        first = trajectory[0]
        self.maatouch.touch_down(0, start_x, start_y, first.get("pressure", 50))

        t0 = time.perf_counter()
        for sample in trajectory[1:-1]:
            target_elapsed_s = sample["t_offset_ms"] / 1000.0
            sleep_s = target_elapsed_s - (time.perf_counter() - t0)
            if sleep_s > 0:
                time.sleep(sleep_s)

            cur_x = max(0, min(w, int(start_x + sample["dx_norm"] * w)))
            cur_y = max(0, min(h, int(start_y + sample["dy_norm"] * h)))
            self.maatouch.touch_move(0, cur_x, cur_y, sample.get("pressure", 50))

        last = trajectory[-1]
        final_sleep = (last["t_offset_ms"] / 1000.0) - (time.perf_counter() - t0)
        if final_sleep > 0:
            time.sleep(final_sleep)
        self.maatouch.touch_up(0)


# =====================================================================
# 3. PARALLEL MULTI-DEVICE FLEET RUNNER
# =====================================================================

def run_fleet_replay(recording_json_path: str, target_serials: List[str], lockstep: bool = True):
    rec_path = Path(recording_json_path)
    parsed_data = json.loads(rec_path.read_text(encoding="utf-8"))
    num_devices = len(target_serials)
    num_steps = len(parsed_data.get("steps", []))

    print(f"[*] Starting synchronized parallel replay of {rec_path.name} across {num_devices} devices...")

    # Barrier 1: Ensures all devices finish u2.connect() + maatouch.start() before Step 1 begins
    start_barrier = threading.Barrier(num_devices)

    # Barrier 2 (Lockstep): Keeps all devices aligned at the start of every step
    lockstep_barriers = (
        [threading.Barrier(num_devices) for _ in range(num_steps)]
        if lockstep and num_devices > 1 else None
    )

    workers = [
        DeviceReplayWorker(
            serial=s,
            recording_path=rec_path,
            parsed_data=parsed_data,
            start_barrier=start_barrier,
            lockstep_barriers=lockstep_barriers
        )
        for s in target_serials
    ]

    with concurrent.futures.ThreadPoolExecutor(max_workers=num_devices) as pool:
        futures = {pool.submit(w.execute_test): w.serial for w in workers}
        for fut in concurrent.futures.as_completed(futures):
            serial = futures[fut]
            try:
                report = fut.result()
                print(f"\n=== Replay Report [{serial}] ===")
                print(json.dumps(report, indent=2))
            except Exception as exc:
                print(f"[!] Device {serial} crashed during replay: {exc}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python replay_engine.py <path/to/recording.json> <SERIAL_1> [SERIAL_2 ...]")
        sys.exit(1)
    run_fleet_replay(sys.argv[1], sys.argv[2:])