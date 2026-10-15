#!/usr/bin/env python3
"""
mighty_grid_priming_orchestrator.py
=====================================================================
MightyGrid Synthetic Monitoring & UX Telemetry Framework
Module: Resilient Master Orchestrator with JSON Telemetry Export
=====================================================================
"""

import sys
import time
import random
import argparse
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone

try:
    from playwright.sync_api import sync_playwright, Page, BrowserContext, TimeoutError as PlaywrightTimeoutError, Error as PlaywrightError
except ImportError:
    print("[!] Dependency missing: playwright")
    sys.exit(1)


def resolve_proxy_location(proxy_server: str) -> dict:
    if not proxy_server:
        return None
        
    print("[*] Pre-flighting proxy to extract physical geolocation...")
    try:
        proxy_handler = urllib.request.ProxyHandler({'http': proxy_server, 'https': proxy_server})
        opener = urllib.request.build_opener(proxy_handler)
        response = opener.open("http://ip-api.com/json/", timeout=10)
        data = json.loads(response.read().decode())
        
        if data.get("status") == "success":
            country = data.get("countryCode", "US")
            locale_map = {
                "GB": "en-GB", "US": "en-US", "DE": "de-DE", 
                "JP": "ja-JP", "IN": "en-IN", "CA": "en-CA", 
                "BR": "pt-BR", "AU": "en-AU", "FR": "fr-FR"
            }
            location_data = {
                "timezone": data.get("timezone", "UTC"),
                "locale": locale_map.get(country, "en-US"),
                "desc": f"{data.get('city')}, {data.get('country')}",
                "lat": data.get("lat"),
                "lon": data.get("lon")
            }
            print(f"    -> Resolved Proxy Origin: {location_data['desc']} ({location_data['timezone']})")
            return location_data
    except Exception as e:
        print(f"[-] Failed to resolve proxy location: {e}")
    return None


def generate_dynamic_profiles(proxy_data: dict = None):
    if proxy_data:
        geo = {"tz": proxy_data["timezone"], "loc": proxy_data["locale"], "desc": proxy_data["desc"]}
    else:
        locations = [
            {"tz": "Asia/Kolkata", "loc": "en-IN", "desc": "Mumbai/Pune, India"},
            {"tz": "Asia/Tokyo", "loc": "ja-JP", "desc": "Tokyo, Japan"},
            {"tz": "Europe/Berlin", "loc": "de-DE", "desc": "Berlin, Germany"},
            {"tz": "America/New_York", "loc": "en-US", "desc": "New York, USA"}
        ]
        geo = random.choice(locations)
        
    geometries = [
        {"w": 1920, "h": 1080, "dpr": 1.0, "mobile": False, "orient": "landscapePrimary"},
        {"w": 1440, "h": 900, "dpr": 2.0, "mobile": False, "orient": "landscapePrimary"}
    ]
    disp = random.choice(geometries)
    is_charging = random.choice([True, False])
    
    profile = {
        "timezone": geo["tz"],
        "locale": geo["loc"],
        "width": disp["w"],
        "height": disp["h"],
        "deviceScaleFactor": disp["dpr"],
        "mobile": disp["mobile"],
        "orientation": disp["orient"],
        "colorScheme": random.choice(["dark", "light"]),
        "battery_charging": is_charging,
        "battery_level": round(random.uniform(0.15, 0.98), 2),
        "battery_chargingTime": random.randint(1200, 7200) if is_charging else "Infinity",
        "battery_dischargingTime": "Infinity" if is_charging else random.randint(3600, 28800)
    }
    
    if proxy_data and proxy_data.get("lat"):
        profile["lat"] = proxy_data["lat"]
        profile["lon"] = proxy_data["lon"]
        
    return profile


def inject_evasion_posture(context: BrowserContext, profile: dict):
    context.add_init_script("""
        Object.defineProperty(navigator, 'hardwareConcurrency', {value: 8, configurable: true});
        Object.defineProperty(navigator, 'deviceMemory', {value: 8, configurable: true});
    """)
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
    dom_orient = "portrait-primary" if "portrait" in profile["orientation"].lower() else "landscape-primary"
    context.add_init_script(f"""
        Object.defineProperty(navigator, 'language', {{value: '{profile["locale"]}', configurable: true}});
        Object.defineProperty(navigator, 'languages', {{value: ['{profile["locale"]}', 'en-US'], configurable: true}});
        if (screen.orientation) {{ Object.defineProperty(screen.orientation, 'type', {{value: '{dom_orient}', configurable: true}}); }}
    """)
    context.add_init_script("""
        const originalAddEventListener = EventTarget.prototype.addEventListener;
        const newAddEventListener = function(type, listener, options) {
            if (['devicemotion', 'deviceorientation'].includes(type)) return;
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


def execute_human_trajectory(page: Page, start: tuple, end: tuple, max_w: int, max_h: int):
    points = [start]
    # Control points still retain your organic variance
    cp1 = (start[0] + (end[0] - start[0]) * 0.3 + random.uniform(-50, 50), start[1] + (end[1] - start[1]) * 0.3 + random.uniform(-50, 50))
    cp2 = (start[0] + (end[0] - start[0]) * 0.7 + random.uniform(-50, 50), start[1] + (end[1] - start[1]) * 0.7 + random.uniform(-50, 50))
    
    for i in range(1, 26):
        t = i / 25
        x = (1-t)**3 * start[0] + 3*(1-t)**2 * t * cp1[0] + 3*(1-t) * t**2 * cp2[0] + t**3 * end[0]
        y = (1-t)**3 * start[1] + 3*(1-t)**2 * t * cp1[1] + 3*(1-t) * t**2 * cp2[1] + t**3 * end[1]
        
        # Clamp coordinates to viewport boundaries to prevent fatal Playwright out-of-bounds exceptions
        safe_x = max(1, min(x, max_w - 1))
        safe_y = max(1, min(y, max_h - 1))
        points.append((safe_x, safe_y))
        
    for x, y in points:
        page.mouse.move(x, y)
        time.sleep(random.uniform(0.005, 0.015))


def extract_web_vitals(page: Page) -> dict:
    return page.evaluate("""
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

def extract_host_baseline(browser) -> dict:
    context = browser.new_context()
    page = context.new_page()
    baseline = page.evaluate("""
    async () => {
        let bat = "API Blocked";
        if (navigator.getBattery) {
            try {
                const b = await navigator.getBattery();
                bat = { charging: b.charging, level: b.level };
            } catch(e) {}
        }
        return {
            timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
            locale: navigator.language,
            width: screen.width,
            height: screen.height,
            deviceScaleFactor: window.devicePixelRatio,
            hardwareConcurrency: navigator.hardwareConcurrency,
            deviceMemory: navigator.deviceMemory,
            battery: bat
        };
    }
    """)
    context.close()
    return baseline

def run_mighty_grid(target_url: str, proxy_server: str = None, max_retries: int = 3):
    print("\n" + "=" * 120)
    print("🚀 MIGHTYGRID MASTER ORCHESTRATOR")
    print("=" * 120)

    proxy_location = resolve_proxy_location(proxy_server)
    
    with sync_playwright() as p:
        launch_args = {
            "channel": "chrome",
            "headless": False, 
            "args": ["--disable-blink-features=AutomationControlled", "--use-gl=angle", "--start-maximized"]
        }
        
        if proxy_server:
            parsed = urllib.parse.urlparse(proxy_server)
            if parsed.username and parsed.password:
                clean_server = f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
                print(f"[*] Binding Network Proxy: {clean_server} (Authenticated)")
                launch_args["proxy"] = {"server": clean_server, "username": parsed.username, "password": parsed.password}
            else:
                print(f"[*] Binding Network Proxy: {proxy_server} (Unauthenticated)")
                launch_args["proxy"] = {"server": proxy_server}
                
        browser = p.chromium.launch(**launch_args)
        
        print("[*] Extracting Host Baseline Telemetry...")
        host_baseline = extract_host_baseline(browser)
        
        final_telemetry_payload = {}

        for attempt in range(1, max_retries + 1):
            print(f"\n=== EXECUTION ATTEMPT {attempt}/{max_retries} ===")
            profile = generate_dynamic_profiles(proxy_location)
            print(f"[*] Generated Target Identity: {profile['timezone']} | {profile['locale']} | Mobile: {profile['mobile']}")

            context_args = {
                'no_viewport': True,
                'is_mobile': profile['mobile'],
                'timezone_id': profile['timezone'],
                'locale': profile['locale'],
                'color_scheme': profile['colorScheme']
            }
            if profile.get('lat') and profile.get('lon'):
                context_args['geolocation'] = {'latitude': profile['lat'], 'longitude': profile['lon']}
                context_args['permissions'] = ['geolocation']
                
            context = browser.new_context(**context_args)
            
            try:
                print("[*] Injecting Evasion Posture (Sensors, Battery, Concurrency)...")
                inject_evasion_posture(context, profile)
                
                page = context.new_page()
                print(f"[*] Navigating to Origin: {target_url}")
                page.goto(target_url, wait_until="load", timeout=20000)

                # Inject these two diagnostic lines
                #print(f"[*] Target Title: {page.title()}")
                #page.screenshot(path="waf_diagnostic_dump.png")

                print("[*] Simulating organic read pattern...")
                for _ in range(random.randint(5, 10)):
                    page.mouse.wheel(0, 400 / 5)
                    time.sleep(random.uniform(0.05, 0.15))

                vitals_1 = extract_web_vitals(page)
                vitals_2 = {"FCP": 0, "LCP": 0, "CLS": 0}
                downstream_url = "N/A"
                
                print("\n[*] Orchestrating interaction to trigger downstream load...")
                visible_links = page.locator("a:visible").all()
                if visible_links:
                    link = random.choice(visible_links[:20])
                    
                    # 1. Force the element into the active viewport
                    link.scroll_into_view_if_needed()
                    
                    # 2. Add an offset to clear sticky enterprise WAF headers
                    page.evaluate("window.scrollBy(0, -150)")
                    page.wait_for_timeout(500)
                    
                    # 3. Recalculate the box ONLY after scrolling and settling
                    box = link.bounding_box()
                    
                    if box and box['y'] > 0 and box['x'] > 0:
                        target_x = box['x'] + (box['width'] / 2)
                        target_y = box['y'] + (box['height'] / 2)
                        start_x = random.randint(10, profile['width'] - 10)
                        start_y = random.randint(10, profile['height'] - 10)
                        
                        print(f"[*] Executing trajectory to X:{target_x:.0f} Y:{target_y:.0f}...")
                        
                        # Pass the viewport limits to the modified trajectory function
                        execute_human_trajectory(page, (start_x, start_y), (target_x, target_y), profile['width'], profile['height'])
                        
                        try:
                            current_url = page.url
                            page.mouse.click(target_x, target_y)
                            
                            # Wait for SPA pushState route change or hard network load
                            page.wait_for_url(lambda url: url != current_url, timeout=5000)
                            
                            downstream_url = page.url
                            vitals_2 = extract_web_vitals(page)
                        except PlaywrightTimeoutError:
                            print("[-] No route change detected (Navigation swallowed by SPA or modal spawned).")
                    else:
                        print("[-] Target element coordinates are invalid or obscured after scrolling. Aborting interaction.")
                else:
                    print("[-] No visible anchor elements found (Likely a raw API response or static page).")
                    if "ip-api" in target_url:
                        try:
                            print(f"\n[*] Raw Origin Response:\n{page.locator('body').inner_text()}")
                        except Exception:
                            pass

                final_telemetry_payload = {
                    "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    "target_url": target_url,
                    "proxy": launch_args.get("proxy", {}).get("server", "None"),
                    "host_baseline": host_baseline,
                    "identity_profile": profile,
                    "landing_vitals": vitals_1,
                    "downstream_url": downstream_url,
                    "downstream_vitals": vitals_2,
                    "status": "SUCCESS"
                }
                
                print("\n=== FINAL TELEMETRY ===")
                print(f"Landing URL:    {target_url}")
                print(f"Landing FCP:    {vitals_1.get('FCP', 0):.1f} ms | LCP: {vitals_1.get('LCP', 0):.1f} ms | CLS: {vitals_1.get('CLS', 0):.4f}")
                if downstream_url != "N/A":
                    print(f"Downstream URL: {downstream_url}")
                    print(f"Downstream FCP: {vitals_2.get('FCP', 0):.1f} ms | LCP: {vitals_2.get('LCP', 0):.1f} ms | CLS: {vitals_2.get('CLS', 0):.4f}")

                context.close()
                break 

            except (PlaywrightTimeoutError, PlaywrightError) as e:
                error_trace = str(e).splitlines()[0]
                print(f"\n[!] Target Closed or Proxy Timeout Encountered: {error_trace}")
                print(f"[-] Tearing down Context Session {attempt}...")
                context.close()
                
                if attempt < max_retries:
                    print(f"[*] Backing off for 3 seconds before next attempt...")
                    time.sleep(3)
                else:
                    print("\n[!] Maximum retry threshold breached. Journey failed.")
                    final_telemetry_payload = {
                        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                        "target_url": target_url,
                        "status": "FAILED",
                        "error": error_trace
                    }

        browser.close()
        
        # Locate project root and ensure the logs directory exists
        # Locate project root (traversing up until past Browser-Telemetry)
        current_dir = os.path.dirname(os.path.abspath(__file__))
        
        # If running from inside priming/, root is two directories up
        # If running from inside Browser-Telemetry/, root is one directory up
        if os.path.basename(current_dir) == "priming":
            root_dir = os.path.abspath(os.path.join(current_dir, "..", ".."))
        elif os.path.basename(current_dir) == "Browser-Telemetry":
            root_dir = os.path.abspath(os.path.join(current_dir, ".."))
        else:
            root_dir = current_dir

        log_dir = os.path.join(root_dir, "logs")
        os.makedirs(log_dir, exist_ok=True)

        export_filename = f"qa_telemetry_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        export_path = os.path.join(log_dir, export_filename)

        with open(export_path, "w", encoding="utf-8") as f:
            json.dump(final_telemetry_payload, f, indent=4)
        print(f"\n[*] Execution logged to: {export_path}")
        os.makedirs(log_dir, exist_ok=True)

        export_filename = f"qa_telemetry_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        export_path = os.path.join(log_dir, export_filename)

        with open(export_path, "w", encoding="utf-8") as f:
            json.dump(final_telemetry_payload, f, indent=4)
        print(f"\n[*] Execution logged and serialized to: {export_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", type=str, default="https://google.com")
    parser.add_argument("--proxy", type=str, help="Proxy URL (e.g., http://user:pass@ip:port)", default=None)
    parser.add_argument("--retries", type=int, help="Maximum execution attempts", default=3)
    args = parser.parse_args()
    run_mighty_grid(args.url, args.proxy, args.retries)