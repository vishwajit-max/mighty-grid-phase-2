#!/usr/bin/env python3
"""
sensor_persona_manager.py
=====================================================================
MightyGrid Sensor Persona Controller (Frida RPC Bridge)

Features:
  - Connects to target device via ADB/USB.
  - Injects sensor_inject.js into com.android.chrome.
  - Dynamically changes motion personas at runtime without restarting Chrome.
  - Can be used interactively or imported directly into master orchestrators.

Usage:
  py telemetry/sensor_persona_manager.py --persona restless
  py telemetry/sensor_persona_manager.py --interactive
=====================================================================
"""

import argparse
import os
import sys
import time


try:
    import frida
except ImportError:
    print("[!] Error: 'frida' package is missing. Install with: pip install frida-tools")
    sys.exit(1)


class SensorPersonaManager:
    def __init__(self, serial: str = "R9ZW904KSXB", package: str = "com.android.chrome", script_path: str = None):
        self.serial = serial
        self.package = package
        self.script_path = script_path or os.path.join(os.path.dirname(__file__), "sensor_inject.js")
        self.session = None
        self.script = None

    def _on_message(self, message, data):
        if message.get("type") == "send":
            print(f"[Frida] {message.get('payload')}")
        elif message.get("type") == "error":
            print(f"[Frida Error] {message.get('description')}")

    def attach_and_inject(self, default_persona: str = "average"):
        print(f"[*] Locating USB device: {self.serial}...")
        try:
            device = frida.get_device(self.serial, timeout=5)
        except Exception:
            # Fallback to default USB device if serial match fails
            device = frida.get_usb_device(timeout=5)

        print(f"[*] Attaching Frida to package '{self.package}'...")
        try:
            self.session = device.attach(self.package)
        except frida.ProcessNotFoundError:
            print(f"[!] Process '{self.package}' is not running.")
            print("    Please launch Chrome on the phone first.")
            sys.exit(1)
        except frida.ServerNotRunningError:
            print("[!] frida-server is not running on the device.")
            print("    Start it via: adb shell su -c '/data/local/tmp/frida-server &'")
            sys.exit(1)

        if not os.path.exists(self.script_path):
            print(f"[!] Script file not found at: {self.script_path}")
            sys.exit(1)

        with open(self.script_path, "r", encoding="utf-8") as f:
            source = f.read()

        print("[*] Loading sensor injection script into Chrome...")
        self.script = self.session.create_script(source)
        self.script.on("message", self._on_message)
        self.script.load()

        # Set default initial persona
        res = self.set_persona(default_persona)
        print(f"[+] Initial Persona: {res}")

    def set_persona(self, name: str) -> str:
        if not self.script:
            raise RuntimeError("Script is not loaded. Call attach_and_inject() first.")
        return self.script.exports_sync.setpersona(name)

    def get_persona(self) -> str:
        if not self.script:
            raise RuntimeError("Script is not loaded. Call attach_and_inject() first.")
        return self.script.exports_sync.getpersona()

    def list_personas(self) -> list:
        if not self.script:
            raise RuntimeError("Script is not loaded. Call attach_and_inject() first.")
        return self.script.exports_sync.listpersonas()

    def interactive_mode(self):
        print("\n" + "=" * 55)
        print("🎮 SENSOR PERSONA INTERACTIVE CONSOLE")
        print("=" * 55)
        print("Available personas:", ", ".join(self.list_personas()))
        print("Commands: Type a persona name to switch, 'status' to check, or 'exit' to quit.\n")

        try:
            while True:
                cmd = input("MightyGrid-Persona > ").strip().lower()
                if not cmd:
                    continue
                if cmd in ["exit", "quit", "q"]:
                    break
                elif cmd == "status":
                    print(f"[*] Active Persona: {self.get_persona()}")
                elif cmd in self.list_personas():
                    result = self.set_persona(cmd)
                    print(f"[+] {result}")
                else:
                    print(f"[!] Unknown command or persona '{cmd}'. Available: {self.list_personas()}")
        except KeyboardInterrupt:
            print("\n[*] Exiting interactive mode...")

    def detach(self):
        if self.session:
            self.session.detach()
            print("[*] Frida session detached.")


def main():
    parser = argparse.ArgumentParser(description="MightyGrid Sensor Persona Manager")
    parser.add_argument("--serial", default="R9ZW904KSXB", help="Target ADB serial")
    parser.add_argument("--package", default="com.android.chrome", help="Target app package")
    parser.add_argument("--persona", default="average", choices=["average", "restless", "careless", "speedy", "shaky"], help="Initial persona to set")
    parser.add_argument("--interactive", action="store_true", help="Launch interactive persona switcher console")
    args = parser.parse_args()

    manager = SensorPersonaManager(serial=args.serial, package=args.package)
    manager.attach_and_inject(default_persona=args.persona)

    if args.interactive:
        manager.interactive_mode()
    else:
        print(f"[*] Persona engine active with '{args.persona}'. Press Ctrl+C to stop.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n[*] Shutting down...")
        finally:
            manager.detach()


if __name__ == "__main__":
    main()