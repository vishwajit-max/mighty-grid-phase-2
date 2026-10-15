#!/usr/bin/env python3
"""
core/unified_dispatcher.py
=====================================================================
Unified Testing Engine Dispatcher for MightyGrid.
Routes execution between Browser (Playwright/CDP) and Android (ADB/Frida).
=====================================================================
"""

import os
import sys
import json
import subprocess
from datetime import datetime
from typing import Dict, Any, Optional

# Resolve root path
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
BROWSER_DIR = os.path.join(BASE_DIR, "Browser-Telemetry")
ANDROID_DIR = os.path.join(BASE_DIR, "tele_without_env", "telemetry")
LOGS_DIR = os.path.join(BASE_DIR, "logs")

os.makedirs(LOGS_DIR, exist_ok=True)

class UnifiedTelemetryRunner:

    @staticmethod
    def run_browser_testing(
        target_url: str = "https://google.com",
        proxy: Optional[str] = None,
        retries: int = 3,
        use_priming: bool = True
    ) -> Dict[str, Any]:
        """
        Executes the Browser Telemetry Suite.
        """
        print(f"[*] Starting Browser Telemetry Job: {target_url}")
        
        script_rel = (
            os.path.join("priming", "mighty_grid_priming_orchestrator.py")
            if use_priming else "mighty_grid_orchestrator.py"
        )
        script_path = os.path.join(BROWSER_DIR, script_rel)
        
        cmd = [sys.executable, script_path, "--url", target_url]
        if proxy:
            cmd.extend(["--proxy", proxy])
        if use_priming:
            cmd.extend(["--retries", str(retries)])

        # Prepare environment with UTF-8 encoding
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"

        try:
            result = subprocess.run(
                cmd,
                cwd=os.path.dirname(script_path),
                capture_output=True,
                text=True,
                encoding="utf-8",      # Explicitly decode output as UTF-8
                errors="replace",       # Prevent crashing on invalid bytes
                env=env,
                check=False
            )
            
            return {
                "platform": "browser",
                "target": target_url,
                "status": "SUCCESS" if result.returncode == 0 else "FAILED",
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "timestamp": datetime.utcnow().isoformat() + "Z"
            }
        except Exception as e:
            return {
                "platform": "browser",
                "target": target_url,
                "status": "ERROR",
                "error": str(e),
                "timestamp": datetime.utcnow().isoformat() + "Z"
            }

    @staticmethod
    def run_android_testing(
        package: str = "com.android.chrome",
        serial: Optional[str] = None,
        persona: str = "average",
        hard_reset: bool = False,
        silent: bool = True
    ) -> Dict[str, Any]:
        """
        Executes the Android Telemetry Suite.
        """
        print(f"[*] Starting Android Telemetry Job on target package: {package}")
        
        script_path = os.path.join(ANDROID_DIR, "mighty_grid_run.py")
        
        cmd = [sys.executable, script_path, "-p", package, "--persona", persona]
        if serial:
            cmd.extend(["-s", serial])
        if hard_reset:
            cmd.append("--hard-reset")
        if silent:
            cmd.append("--silent")

        try:
            result = subprocess.run(
                cmd,
                cwd=ANDROID_DIR,
                capture_output=True,
                text=True,
                check=False
            )
            
            return {
                "platform": "android",
                "target": package,
                "status": "SUCCESS" if result.returncode == 0 else "FAILED",
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "timestamp": datetime.utcnow().isoformat() + "Z"
            }
        except Exception as e:
            return {
                "platform": "android",
                "target": package,
                "status": "ERROR",
                "error": str(e),
                "timestamp": datetime.utcnow().isoformat() + "Z"
            }

    @classmethod
    def execute(cls, selection: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Main decision branch called by CLI or Web UI.
        selection: 'browser' | 'android'
        """
        selection_key = selection.strip().lower()

        if selection_key == "browser":
            return cls.run_browser_testing(
                target_url=params.get("url", "https://google.com"),
                proxy=params.get("proxy"),
                retries=params.get("retries", 3),
                use_priming=params.get("use_priming", True)
            )
        elif selection_key == "android":
            return cls.run_android_testing(
                package=params.get("package", "com.android.chrome"),
                serial=params.get("serial"),
                persona=params.get("persona", "average"),
                hard_reset=params.get("hard_reset", False),
                silent=params.get("silent", True)
            )
        else:
            raise ValueError(f"Invalid testing engine '{selection}'. Choose 'browser' or 'android'.")

    @staticmethod
    def run_manual_browser(target_url: str, proxy: Optional[str] = None):
        """
        Executes the Manual Browser Evasion Suite interactively.
        Does not capture output so the terminal's stdin remains open for the input() pause.
        """
        print(f"[*] Starting Manual Browser Session: {target_url}")
        
        script_path = os.path.join(BROWSER_DIR, "priming", "manual_browser_evasion.py")
        
        cmd = [sys.executable, script_path]
        if target_url:
            cmd.extend(["--url", target_url])
        if proxy:
            cmd.extend(["--proxy", proxy])

        # We must use call() without capture_output so the user can press ENTER to close it
        try:
            subprocess.call(
                cmd,
                cwd=os.path.dirname(script_path)
            )
        except KeyboardInterrupt:
            print("\n[*] Manual session terminated by user.")