#!/usr/bin/env python3
"""
manual_evasion_browser.py
=====================================================================
MightyGrid Standalone Manual Testing Module
Launches a maximized, desktop-only Playwright session with full CDP 
evasion and optional authenticated proxy routing.
=====================================================================
"""

import sys
import json
import random
import argparse
import urllib.parse
import urllib.request
import os
from datetime import datetime, timezone
from playwright.sync_api import sync_playwright


def resolve_proxy_location(proxy_server: str) -> dict:
    """Pre-flights the proxy to extract its physical geolocation for DOM matching."""
    if not proxy_server:
        return None
        
    print("[*] Pre-flighting proxy to extract physical geolocation...")
    try:
        proxy_handler = urllib.request.ProxyHandler({'http': proxy_server, 'https': proxy_server})
        opener = urllib.request.build_opener(proxy_handler)
        response = opener.open("http://ip-api.com/json/", timeout=10)
        data = json.loads(response.read().decode())
        
        if data.get("status") == "success":
            locale_map = {
                "GB": "en-GB", "US": "en-US", "DE": "de-DE", 
                "JP": "ja-JP", "IN": "en-IN", "CA": "en-CA", 
                "BR": "pt-BR", "AU": "en-AU", "FR": "fr-FR"
            }
            return {
                "timezone": data.get("timezone", "UTC"),
                "locale": locale_map.get(data.get("countryCode", "US"), "en-US"),
                "desc": f"{data.get('city')}, {data.get('country')}"
            }
    except Exception as e:
        print(f"[-] Failed to resolve proxy location: {e}")
    return None


def generate_desktop_profile(proxy_data: dict = None) -> dict:
    """Generates a strictly desktop-bound identity profile."""
    if proxy_data:
        tz, loc = proxy_data["timezone"], proxy_data["locale"]
    else:
        # Default fallbacks if no proxy is used
        locations = [
            {"tz": "Asia/Kolkata", "loc": "en-IN"},
            {"tz": "America/New_York", "loc": "en-US"},
            {"tz": "Europe/London", "loc": "en-GB"}
        ]
        choice = random.choice(locations)
        tz, loc = choice["tz"], choice["loc"]

    is_charging = random.choice([True, False])
    
    return {
        "timezone": tz,
        "locale": loc,
        "colorScheme": random.choice(["dark", "light"]),
        "battery_charging": is_charging,
        "battery_level": round(random.uniform(0.15, 0.98), 2),
        "battery_chargingTime": random.randint(1200, 7200) if is_charging else "Infinity",
        "battery_dischargingTime": "Infinity" if is_charging else random.randint(3600, 28800)
    }


def inject_evasion_posture(context, profile: dict):
    """Injects native CDP overrides for hardware concurrency, battery, sensors, and languages."""
    # 1. Hardware Metrics
    context.add_init_script("""
        Object.defineProperty(navigator, 'hardwareConcurrency', {value: 8, configurable: true});
        Object.defineProperty(navigator, 'deviceMemory', {value: 8, configurable: true});
    """)
    
    # 2. Battery API Spoofing
    context.add_init_script(f"""
        const mockBattery = {{
            charging: {str(profile["battery_charging"]).lower()},
            chargingTime: {profile["battery_chargingTime"]},
            dischargingTime: {profile["battery_dischargingTime"]},
            level: {profile["battery_level"]},
            addEventListener: function() {{}},
            removeEventListener: function() {{}}
        }};
        Object.defineProperty(navigator, 'getBattery', {{value: () => Promise.resolve(mockBattery), configurable: true}});
    """)
    
    # 3. Locale & Language
    context.add_init_script(f"""
        Object.defineProperty(navigator, 'language', {{value: '{profile["locale"]}', configurable: true}});
        Object.defineProperty(navigator, 'languages', {{value: ['{profile["locale"]}', 'en-US'], configurable: true}});
    """)
    
    # 4. Strict Sensor Ghosting (Prevent Desktop Motion Leaks)
    context.add_init_script("""
        const originalAddEventListener = EventTarget.prototype.addEventListener;
        const newAddEventListener = function(type, listener, options) {
            if (['devicemotion', 'deviceorientation', 'deviceorientationabsolute'].includes(type)) return;
            return originalAddEventListener.apply(this, arguments);
        };
        Object.defineProperty(EventTarget.prototype, 'addEventListener', { value: newAddEventListener, configurable: true });
        Object.defineProperty(Function.prototype, 'toString', {
            value: function() {
                if (this === newAddEventListener) return "function addEventListener() { [native code] }";
                return Function.prototype.toString.apply(this, arguments);
            }, configurable: true
        });
    """)


def launch_manual_browser(target_url: str, proxy_server: str = None):
    print("\n" + "=" * 100)
    print("🚀 MIGHTYGRID MANUAL BROWSER MODULE (DESKTOP MODE)")
    print("=" * 100)

    proxy_location = resolve_proxy_location(proxy_server)
    profile = generate_desktop_profile(proxy_location)

    print(f"\n[*] Target Identity: {profile['timezone']} | {profile['locale']} | Theme: {profile['colorScheme']}")

    with sync_playwright() as p:
        launch_args = {
            "channel": "chrome",
            "headless": False,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--use-gl=angle",
                "--start-maximized"
            ]
        }

        if proxy_server:
            parsed = urllib.parse.urlparse(proxy_server)
            if parsed.username and parsed.password:
                clean_server = f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
                print(f"[*] Binding Authenticated Proxy: {clean_server}")
                launch_args["proxy"] = {
                    "server": clean_server,
                    "username": parsed.username,
                    "password": parsed.password
                }
            else:
                print(f"[*] Binding Unauthenticated Proxy: {proxy_server}")
                launch_args["proxy"] = {"server": proxy_server}
        else:
            print("[*] No proxy specified. Routing through local host IP.")

        browser = p.chromium.launch(**launch_args)

        context_args = {
            "no_viewport": True,
            "timezone_id": profile["timezone"],
            "locale": profile["locale"],
            "color_scheme": profile["colorScheme"],
            "permissions": ["geolocation"]
        }
        
        context = browser.new_context(**context_args)
        
        print("[*] Injecting CDP Evasion Posture...")
        inject_evasion_posture(context, profile)

        page = context.new_page()
        
        if target_url:
            print(f"[*] Navigating to Origin: {target_url}")
            try:
                page.goto(target_url, wait_until="load")
            except Exception as e:
                print(f"[-] Warning during navigation: {e}")
        else:
            print("[*] No URL specified. Navigating to default home screen.")
            page.goto("https://www.google.com", wait_until="load")
            

        print("\n" + "=" * 100)
        print("[!] BROWSER IS ACTIVE. You have manual control.")
        print("[!] Do not close the browser window with the 'X' button.")
        print("[!] To shut down cleanly and kill the context, press ENTER in this terminal.")
        print("=" * 100 + "\n")
        
        # Suspend script silently
        input()

        print("\n[*] Extracting session telemetry before shutdown...")
        try:
            # 1. Grab the currently active tab (handles users opening new tabs)
            if not context.pages:
                raise Exception("All browser tabs were closed by the user.")
            
            active_page = context.pages[-1]
            
            # 2. Wait for any mid-flight manual navigations to settle
            try:
                active_page.wait_for_load_state("domcontentloaded", timeout=3000)
            except Exception:
                pass # Timeout means it might be stuck loading; try evaluating anyway
            
            current_url = active_page.url
            
            # 3. Safely evaluate vitals
            try:
                vitals = active_page.evaluate("""
                async () => {
                    return new Promise((resolve) => {
                        let m = { FCP: 0, LCP: 0, CLS: 0 };
                        const p = performance.getEntriesByName('first-contentful-paint');
                        if (p.length > 0) m.FCP = p[0].startTime;
                        const l = new PerformanceObserver((list) => { const e = list.getEntries(); if (e.length > 0) m.LCP = e[e.length - 1].startTime; });
                        try { l.observe({ type: 'largest-contentful-paint', buffered: true }); } catch(e){}
                        const c = new PerformanceObserver((list) => { for (const e of list.getEntries()) if (!e.hadRecentInput) m.CLS += e.value; });
                        try { c.observe({ type: 'layout-shift', buffered: true }); } catch(e){}
                        setTimeout(() => { l.disconnect(); c.disconnect(); resolve(m); }, 250);
                    });
                }
                """)
            except Exception as eval_err:
                print(f"[-] Core Web Vitals extraction bypassed (context shifted during read): {eval_err}")
                vitals = {"FCP": 0, "LCP": 0, "CLS": 0}
            
            payload = {
                "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "landing_url": target_url or "https://www.google.com",
                "final_url": current_url,
                "identity_profile": profile,
                "vitals": vitals,
                "status": "MANUAL_SUCCESS"
            }

            # Locate project root (traversing up until past Browser-Telemetry)
            current_dir = os.path.dirname(os.path.abspath(__file__))
            if os.path.basename(current_dir) == "priming":
                root_dir = os.path.abspath(os.path.join(current_dir, "..", ".."))
            elif os.path.basename(current_dir) == "Browser-Telemetry":
                root_dir = os.path.abspath(os.path.join(current_dir, ".."))
            else:
                root_dir = current_dir

            log_dir = os.path.join(root_dir, "logs")
            os.makedirs(log_dir, exist_ok=True)
            
            export_path = os.path.join(log_dir, f"manual_telemetry_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
            with open(export_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=4)
            print(f"[*] Manual session logged and serialized to: {export_path}")
            
        except Exception as e:
            print(f"[-] Failed to extract telemetry: {e}")

        print("\n[*] Terminating browser context...")
        context.close()
        browser.close()
        print("[*] Exit complete.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", type=str, default=None, help="Target URL (optional)")
    parser.add_argument("--proxy", type=str, default=None, help="Proxy URL (e.g., http://user:pass@ip:port)")
    args = parser.parse_args()
    
    launch_manual_browser(args.url, args.proxy)