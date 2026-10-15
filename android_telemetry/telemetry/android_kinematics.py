#!/usr/bin/env python3
"""
android_kinematics.py
=====================================================================
Translates BiometricPersona traits into physical MaaTouch coordinates,
injecting tremors, curve bias, and scatter directly over ADB.
=====================================================================
"""

import time
import math
import random
import maatouch
import personas

class AndroidKinematics:
    def __init__(self, serial: str, persona_name: str):
        self.serial = serial
        self.persona = personas.generate_persona(archetype=persona_name)
        
        print(f"[*] Initializing MaaTouch Daemon for persona: {self.persona.name}...")
        self.mt = maatouch.get(serial)
        
        if not self.mt:
            print("[-] Warning: MaaTouch daemon failed to initialize. Kinematics bypassed.")

    def is_active(self):
        return self.mt is not None

    def _apply_scatter(self, x, y):
        """Applies pixel scatter based on the persona's precision trait."""
        sx = x + random.gauss(0, self.persona.scatter_px)
        sy = y + random.gauss(0, self.persona.scatter_px)
        return sx, sy

    def tap(self, x: float, y: float):
        """Executes a single tap with human reaction and dwell timing."""
        if not self.is_active(): return False
        
        time.sleep(self.persona.reaction_ms / 1000.0)
        tx, ty = self._apply_scatter(x, y)
        hold_ms = 55 * self.persona.dwell_scale
        
        print(f"[*] {self.persona.name} tapping at X:{tx:.0f} Y:{ty:.0f} (Dwell: {hold_ms:.0f}ms)")
        return self.mt.tap(tx, ty, hold_ms=hold_ms)

    def swipe(self, start_x: float, start_y: float, end_x: float, end_y: float):
        """Executes a swipe gesture with injected tremor and curved trajectories."""
        if not self.is_active(): return False

        time.sleep(self.persona.reaction_ms / 1000.0)
        sx, sy = self._apply_scatter(start_x, start_y)
        ex, ey = self._apply_scatter(end_x, end_y)

        # Higher flick vigor means fewer, faster steps
        steps = int(random.uniform(20, 35) / self.persona.flick_vigor)
        moves = []
        
        for i in range(1, steps + 1):
            t = i / steps
            
            # Apply handedness curve bias (arcs slightly on the X axis during vertical scrolls)
            cx = sx + (ex - sx) * t + (math.sin(t * math.pi) * 60 * self.persona.curve_bias)
            cy = sy + (ey - sy) * t
            
            # Inject micro-tremor based on persona steadiness
            cx += random.gauss(0, self.persona.tremor_scale * 1.5)
            cy += random.gauss(0, self.persona.tremor_scale * 1.5)
            
            # Burstiness dictates the timing variance between interpolation frames
            wait_ms = random.uniform(8, 16) / self.persona.burstiness
            moves.append((cx, cy, wait_ms))
            
        hold_before_up = 20 * self.persona.dwell_scale
        
        print(f"[*] {self.persona.name} swiping to X:{ex:.0f} Y:{ey:.0f}")
        return self.mt.swipe((sx, sy), moves, hold_before_up_ms=hold_before_up)

    def organic_scroll_burst(self, screen_width=1080, screen_height=2400):
        """Executes a sequence of reading scrolls based on the persona's patience."""
        if not self.is_active(): return

        scroll_count = self.persona.retry_budget
        print(f"\n[*] Executing organic scroll burst ({scroll_count} interactions)...")
        
        for _ in range(scroll_count):
            start_x = screen_width / 2 + random.uniform(-100, 100)
            start_y = screen_height * 0.75 + random.uniform(-50, 50)
            
            # Fling bias determines scroll distance
            distance = random.uniform(300, 800) * self.persona.fling_bias
            end_y = start_y - distance
            
            self.swipe(start_x, start_y, start_x, end_y)
            time.sleep(random.uniform(0.5, 1.5) / self.persona.curiosity)