#!/usr/bin/env python3
"""
main.py - Interactive CLI interface
"""

from core.unified_dispatcher import UnifiedTelemetryRunner

def main():
    print("=" * 60)
    print("🚀 MIGHTYGRID UNIFIED ENGINE SELECTION")
    print("=" * 60)
    print("1. Browser Telemetry (Playwright / CDP)")
    print("2. Android Telemetry (ADB / Frida / Native Sensors)")
    
    choice = input("\nSelect platform to test [1/2]: ").strip()
    
    if choice == "1":
        mode = input("Select mode [1] Automated [2] Manual: ").strip()
        url = input("Target URL (default: https://google.com): ").strip() or "https://google.com"
        proxy = input("Proxy URL (Leave empty if none): ").strip() or None
        
        if mode == "2":
            # Manual mode blocks the terminal and relies on the user to hit ENTER
            UnifiedTelemetryRunner.run_manual_browser(url, proxy)
        else:
            # Automated mode executes silently and returns a structured JSON report
            report = UnifiedTelemetryRunner.execute(
                selection="browser",
                params={"url": url, "proxy": proxy}
            )
            print("\n=== EXECUTION RESULT ===")
            print(f"Status: {report.get('status')}")
            if report.get("stdout"):
                print("\n--- STDOUT ---")
                print(report.get("stdout"))
            if report.get("stderr"):
                print("\n--- STDERR ---")
                print(report.get("stderr"))
            if report.get("error"):
                print("\n--- RUNNER ERROR ---")
                print(report.get("error"))

    elif choice == "2":
        pkg = input("Android Target Package (default: com.android.chrome): ").strip() or "com.android.chrome"
        serial = input("Target Device Serial (leave empty for default): ").strip() or None
        
        report = UnifiedTelemetryRunner.execute(
            selection="android",
            params={"package": pkg, "serial": serial, "silent": True}
        )
        print("\n=== EXECUTION RESULT ===")
        print(f"Status: {report.get('status')}")
        if report.get("stdout"):
            print(report.get("stdout"))
        if report.get("stderr"):
            print("\n--- STDERR ---")
            print(report.get("stderr"))

    else:
        print("[!] Invalid option. Exiting.")

if __name__ == "__main__":
    main()