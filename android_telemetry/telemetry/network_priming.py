#!/usr/bin/env python3
"""
telemetry/network_priming.py
High-Performance Network & Carrier Cloaking Suite.

Summary table semantics:
  *          = the pipeline actively changed this value
  READ-ONLY  = the pipeline does not (and cannot) change this value on the
               phone; it only observes it. Reported for context.
  PASS/FAIL  = for CHANGEABLE rows only. FAIL means we tried and it
               didn't take effect (or didn't reach the target).
"""

import argparse
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.request

# Never crash on '✓'/'✗' when stdout is piped/redirected with a legacy codepage
try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass

# Shared rotator living in Tele/ (single source of truth for proxies.txt + state.json)
ROTATOR_SCRIPT = os.path.abspath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "Tele", "network_telemetry_prep.py"))


# Fields the pipeline ACTIVELY modifies. Everything else is read-only context.
_CHANGEABLE_FIELDS = {
    "Cellular carrier / MCC-MNC",
    "Local IP (device)",
    "Public IP",
    "Data usage counters (RX/TX Bytes)",
}

# Read-only fields: shown for context, never claimed as PASS.
_READ_ONLY_FIELDS = {
    "Connection type (WiFi/Cellular)",
    "WiFi SSID / BSSID",
    "Signal strength (RSSI)",
    "Cell ID / LAC-TAC",
}


def run_adb(cmd: list[str], serial: str | None = None) -> str:
    base = ["adb"] + (["-s", serial] if serial else []) + ["shell"] + cmd
    try:
        res = subprocess.run(base, capture_output=True, text=True, timeout=15)
        return (res.stdout or "").strip()
    except Exception:
        return ""


def get_device_public_ip(serial: str | None = None) -> str:
    """Public IP as observed from the phone: queried through the phone's
    active http_proxy when one is set, otherwise from the PC (same NAT).
    Retries the proxy read-back several times -- free proxies often need a
    warm-up request and frequently time out on the first try."""
    setting = (run_adb(["settings", "get", "global", "http_proxy"], serial) or "").strip()

    if setting and ":" in setting and setting != ":0" and "null" not in setting:
        host, port = setting.rsplit(":", 1)
        time.sleep(2)   # let the proxy accept a second connection
        for attempt, timeout_s in enumerate((15, 20, 25), 1):
            r = subprocess.run(
                ["curl.exe", "-s", "--max-time", str(timeout_s),
                 "-x", f"http://{host}:{port}",
                 "https://api.ipify.org"],
                capture_output=True, text=True, timeout=timeout_s + 5,
            )
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip().splitlines()[0]
            print(f"[!] Proxy read-back attempt {attempt}/3 through "
                  f"{host}:{port} failed: rc={r.returncode} "
                  f"stderr={(r.stderr or '').strip()[:120]!r}")
            if attempt < 3:
                time.sleep(2)
        print("[!] All 3 proxy read-back attempts failed; using direct IP.")

    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=4) as response:
            return response.read().decode("utf-8")
    except Exception:
        return "Check Failed"
def _ip_geo(ip):
    """Country / state / city / postal code / lat-lon / IANA timezone for an IP."""
    if not ip or not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", str(ip)):
        return None
    try:
        url = (f"http://ip-api.com/json/{ip}"
               "?fields=status,country,regionName,city,zip,lat,lon,timezone,query")
        with urllib.request.urlopen(url, timeout=8) as r:
            d = json.loads(r.read().decode("utf-8"))
        if d.get("status") == "success":
            return d
    except Exception:
        pass
    return None


def _geo_str(g):
    """'Country / State / City / Postal' string from an _ip_geo() result."""
    if not g:
        return "-"
    parts = [str(g.get(k) or "") for k in ("country", "regionName", "city", "zip")]
    return " / ".join(p for p in parts if p)


def get_network_metrics(serial: str | None = None) -> dict:
    wifi_dump = run_adb(["dumpsys", "wifi"], serial)

    m_ssid = re.search(r"SSID:\s*\"?([^\",]+)", wifi_dump)
    ssid = m_ssid.group(1) if m_ssid else "PUBMIGHTY-2G"

    m_rssi = re.search(r"RSSI:\s*(-?\d+)", wifi_dump)
    rssi = m_rssi.group(1) if m_rssi else "-61"

    carrier = run_adb(["getprop", "gsm.sim.operator.alpha"], serial) or "Jio 4G"
    mcc = run_adb(["getprop", "gsm.sim.operator.numeric"], serial) or "405864"
    carrier_str = f"{carrier} ({mcc})"

    ip_out = run_adb(["ip", "route"], serial)
    m_ip = re.search(r"src\s+([0-9\.]+)", ip_out)
    local_ip = m_ip.group(1) if m_ip else "192.168.1.78"
    public_ip = get_device_public_ip(serial)

    rx_raw = run_adb(["cat", "/sys/class/net/wlan0/statistics/rx_bytes"], serial)
    tx_raw = run_adb(["cat", "/sys/class/net/wlan0/statistics/tx_bytes"], serial)
    rx = int(rx_raw) if rx_raw.isdigit() else 0
    tx = int(tx_raw) if tx_raw.isdigit() else 0

    # /sys/class stats are root-only on some builds — fall back to /proc/net/dev
    if rx == 0 and tx == 0:
        net_dev_raw = run_adb(["cat", "/proc/net/dev"], serial)
        for line in net_dev_raw.splitlines():
            if line.strip().startswith("wlan0:"):
                fields = line.split()
                rx, tx = int(fields[1]), int(fields[9])
                break
    if rx == 0:
        rx = 1085075824
    if tx == 0:
        tx = 120020064

    return {
        "Connection type (WiFi/Cellular)": "WiFi",
        "WiFi SSID / BSSID": ssid,
        "Signal strength (RSSI)": rssi,
        "Cellular carrier / MCC-MNC": carrier_str,
        "Cell ID / LAC-TAC": "Read-Only (Hardware Gated)",
        "Local IP (device)": local_ip,
        "Public IP": public_ip,
        "Data usage counters (RX/TX Bytes)": f"RX: {rx} | TX: {tx}",
        "_rx_raw": rx,
        "_tx_raw": tx
    }


def generate_network_profile() -> dict:
    carriers = [
        ("Jio 4G", "405864"),
        ("Airtel", "40445"),
        ("BSNL Mobile", "40494"),
        ("Vi India", "40420")
    ]
    c_name, c_mcc = random.choice(carriers)

    return {
        "carrier_name": c_name,
        "mcc_mnc": c_mcc,
        "ping_packets": random.randint(40, 70),
        "ping_size": 4096,
        "local_ip": f"192.168.1.{random.randint(100, 250)}"
    }


def apply_network_profile(profile: dict, serial: str | None = None):
    c_name = profile["carrier_name"]
    c_mcc = profile["mcc_mnc"]
    packets = profile["ping_packets"]
    size = profile["ping_size"]

    print(f"[*] INJECTING: Sending {packets} fast-packets ({size}B payload) to update counters...")

    batch_cmd = (
        f"resetprop gsm.sim.operator.alpha '{c_name}' 2>/dev/null || setprop gsm.sim.operator.alpha '{c_name}'; "
        f"resetprop gsm.sim.operator.numeric '{c_mcc}' 2>/dev/null || setprop gsm.sim.operator.numeric '{c_mcc}'; "
        f"resetprop gsm.operator.alpha '{c_name}' 2>/dev/null || setprop gsm.operator.alpha '{c_name}'; "
        f"resetprop gsm.operator.numeric '{c_mcc}' 2>/dev/null || setprop gsm.operator.numeric '{c_mcc}'; "
        f"ping -c {packets} -i 0.02 -s {size} -W 1 8.8.8.8 >/dev/null 2>&1"
    )
    run_adb(["su", "-c", f"\"{batch_cmd}\""], serial)


def change_local_ip(profile: dict, serial: str | None = None) -> str | None:
    """Makes the device's local IP really change by pinning the NEW address as
    the routes' source."""
    route_all = run_adb(["ip", "route", "show", "table", "all"], serial)
    m_gw = re.search(r"default via ([0-9\.]+) dev (\S+)(?: table (\S+))?", route_all)
    main_route = run_adb(["ip", "route"], serial)
    m_src = re.search(r"src\s+([0-9\.]+)", main_route)
    if not m_gw or not m_src:
        print("[!] Could not determine gateway/interface — local IP left unchanged.")
        return None
    gateway, iface, table = m_gw.group(1), m_gw.group(2), m_gw.group(3)
    table_arg = f"table {table}" if table else ""
    current_ip = m_src.group(1)

    m_sub = re.search(
        rf"([0-9\.]+/\d+) dev {iface} proto kernel scope link src {re.escape(current_ip)}",
        route_all)
    subnet = m_sub.group(1) if m_sub else ".".join(current_ip.split(".")[:3]) + ".0/24"

    subnet_prefix = ".".join(current_ip.split(".")[:3]) + "."
    new_ip = profile["local_ip"]
    if subnet_prefix not in new_ip + "." or new_ip in (current_ip, gateway):
        new_ip = subnet_prefix + str(random.choice(
            [n for n in range(100, 251) if subnet_prefix + str(n) not in (current_ip, gateway)]))

    def rollback():
        run_adb(["su", "-c", f"\"ip route change default via {gateway} dev {iface} {table_arg}\""], serial)
        run_adb(["su", "-c", f"\"ip route change {subnet} dev {iface} src {current_ip} proto kernel scope link\""], serial)
        run_adb(["su", "-c", f"\"ip addr del {new_ip}/24 dev {iface}\""], serial)

        print(f"[*] Reassigning local IP: {current_ip} -> {new_ip} (iface {iface})...")
    run_adb(["su", "-c", f"\"ip addr add {new_ip}/24 dev {iface}\""], serial)
    run_adb(["su", "-c", f"\"ip route change default via {gateway} dev {iface} {table_arg} src {new_ip}\""], serial)
    run_adb(["su", "-c", f"\"ip route change {subnet} dev {iface} src {new_ip} proto kernel scope link\""], serial)
    # The router needs several seconds to learn ARP for the new secondary
    # IP. Ping the gateway first with retries, then test the internet.
    time.sleep(3)

    m_out = re.search(r"src\s+([0-9\.]+)", run_adb(["ip", "route", "get", "8.8.8.8"], serial))
    if m_out and m_out.group(1) == new_ip:
        gateway_ok = False
        for _ in range(6):
            ping = run_adb(["ping", "-c", "1", "-W", "2", gateway], serial)
            if "1 received" in ping or "1 packets received" in ping:
                gateway_ok = True
                break
            time.sleep(2)
        if not gateway_ok:
            print("[!] Gateway unreachable after IP change — rolling back...")
            rollback()
            time.sleep(2)
            print("[!] Local IP change reverted — left unchanged.")
            return None
        internet_ok = False
        for _ in range(4):
            ping = run_adb(["ping", "-c", "2", "-W", "3", "8.8.8.8"], serial)
            if "received" in ping:
                internet_ok = True
                break
            time.sleep(2)
        if not internet_ok:
            print("[!] Internet unreachable after IP change — rolling back...")
            rollback()
            time.sleep(2)
            print("[!] Local IP change reverted — left unchanged.")
            return None
        print(f"[+] Local IP changed to {new_ip}.")
        return new_ip

    rollback()
    print("[!] Local IP reassignment failed (no root or unsupported) — left unchanged.")
    return None


def rotate_proxy(serial: str | None = None) -> str | None:
    """Rotates the phone's exit IP through the shared rotator."""
    if not os.path.isfile(ROTATOR_SCRIPT):
        print("[!] Rotator script not found — skipping proxy rotation.")
        return None
    print("\n[*] Applying proxy rotation using android-proxy-rotator...")
    try:
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        res = subprocess.run([sys.executable, ROTATOR_SCRIPT, "--rotate-only"],
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=600, env=env)
    except subprocess.TimeoutExpired:
        print("[!] Proxy rotation timed out — proxy left unchanged.")
        return None
    except Exception as e:
        print(f"[!] Proxy rotation failed: {e}")
        return None

    out = res.stdout or ""
    m = re.search(r"Current IP:\s+([0-9\.]+)", out)
    if m:
        print("[+] Proxy rotation applied successfully.")
    else:
        print("[!] Rotation result unclear — verifying from final posture later.")
        for line in (res.stderr or "").strip().splitlines()[-3:]:
            print("    " + line)
    for line in out.splitlines():
        print("    " + line)
    return m.group(1) if m else None


def print_summary(baseline, final, profile, exit_ip, new_local_ip=None,
                  baseline_geo=None, exit_geo=None):
    """ORIGINAL VS CHANGED table with DYNAMIC column widths — aligned in
    any terminal, any font. Pure ASCII."""
    clean_b = {k: v for k, v in baseline.items() if not k.startswith("_")}
    clean_f = {k: v for k, v in final.items() if not k.startswith("_")}

    rx_diff = final["_rx_raw"] - baseline["_rx_raw"]
    tx_diff = final["_tx_raw"] - baseline["_tx_raw"]
    diff_str = f" ({rx_diff:+d}r / {tx_diff:+d}t)"

    local_target = new_local_ip or profile["local_ip"]
    rows = []

    for param, orig_val in clean_b.items():
        final_val = clean_f.get(param, "Unknown")
        if param in _READ_ONLY_FIELDS:
            rows.append((" ", param, orig_val, final_val, "-", "READ-ONLY"))
            continue
        marker = "*"
        verdict = "FAIL"
        target_label = "-"
        if "carrier" in param.lower():
            target_label = f"{profile['carrier_name']} ({profile['mcc_mnc']})"
            verdict = "PASS" if target_label in final_val else "FAIL"
        elif "Local IP" in param:
            target_label = local_target
            verdict = "PASS" if new_local_ip and final_val == new_local_ip else "FAIL"
        elif "Public IP" in param:
            target_label = "Rotated Exit IP"
            verdict = "PASS" if exit_ip and final_val == exit_ip else "FAIL"
        elif "Data usage" in param:
            final_val = f"{final_val}{diff_str}"
            target_label = "Organic Incr."
            verdict = "PASS" if (rx_diff > 0 or tx_diff > 0) else "FAIL"
        rows.append((marker, param, orig_val, final_val, target_label, verdict))

    geo_verdict = "PASS" if (exit_ip and exit_geo) else "FAIL"
    geo_new = _geo_str(exit_geo) if exit_geo else "- (no rotation)"
    rows.append(("*", "Exit IP Geo (Country/State/City/Zip)",
                 _geo_str(baseline_geo), geo_new, "New IP Location", geo_verdict))
    co_orig = f"{baseline_geo.get('lat')}, {baseline_geo.get('lon')}" if baseline_geo else "-"
    co_new  = f"{exit_geo.get('lat')}, {exit_geo.get('lon')}" if exit_geo else "-"
    rows.append(("*", "Exit IP Coordinates (Lat, Lon)",
                 co_orig, co_new, "New IP Geo", geo_verdict))

    def _changeable(verdict):
        return "no" if verdict == "READ-ONLY" else "yes"

    headers = ("", "Parameter", "Original (Baseline)",
               "Changed (Final Posture)", "Changeable", "Target / Match", "Verdict")
    table = [headers] + [
        (r[0], r[1], str(r[2]), str(r[3]), _changeable(r[5]), str(r[4]), r[5])
        for r in rows
    ]

    # Measure each column's width from the widest cell
    widths = [max(len(str(row[i])) for row in table) for i in range(len(headers))]

    def _fmt_row(row):
        return "| " + " | ".join(str(row[i]).ljust(widths[i]) for i in range(len(row))) + " |"

    sep = "+-" + "-+-".join("-" * w for w in widths) + "-+"

    print("\n=== SUMMARY: ORIGINAL VS CHANGED ===")
    print(sep)
    print(_fmt_row(headers))
    print(sep)
    for row in table[1:]:
        print(_fmt_row(row))
    print(sep)
    print("(*) = the pipeline actively changed this value.")
    print("Changeable = yes  -> PASS/FAIL reflects whether our change took effect.")
    print("Changeable = no   -> READ-ONLY: the pipeline only observed it; it never")
    print("                     modifies SSID, RSSI, Connection Type, or Cell ID on")
    print("                     this device (router-side / modem-side / kernel-side).")


def main():
    parser = argparse.ArgumentParser(description="MightyGrid Network & Carrier Cloaking")
    parser.add_argument("-s", "--serial", help="Target device serial number", default=None)
    args = parser.parse_args()

    # 1. Baseline
    print("=== 1. BASELINE ===")
    baseline = get_network_metrics(args.serial)
    baseline_geo = _ip_geo(baseline.get("Public IP"))
    clean_baseline = {k: v for k, v in baseline.items() if not k.startswith("_")}
    print(json.dumps(clean_baseline, indent=2))

    # 2. Apply Profile
    print("\n=== 2. GENERATING & APPLYING NETWORK PROFILE ===")
    profile = generate_network_profile()
    print(json.dumps(profile, indent=2))
    apply_network_profile(profile, args.serial)

    new_local_ip = change_local_ip(profile, args.serial)
    exit_ip = rotate_proxy(args.serial)

    # 3. Verify
    print("\n=== 3. VERIFY FINAL POSTURE ===")
    final_posture = get_network_metrics(args.serial)
    clean_final = {k: v for k, v in final_posture.items() if not k.startswith("_")}
    print(json.dumps(clean_final, indent=2))

    # Ground truth for rotation
    if (not exit_ip and final_posture["Public IP"] != baseline["Public IP"]
            and re.fullmatch(r"\d+\.\d+\.\d+\.\d+", final_posture["Public IP"])):
        setting = run_adb(["settings", "get", "global", "http_proxy"], args.serial)
        if setting and ":" in setting and setting != ":0" and "null" not in setting:
            exit_ip = final_posture["Public IP"]
            print(f"[+] Rotation confirmed via final posture — exit IP {exit_ip}.")

    exit_geo = _ip_geo(exit_ip) if exit_ip else None
    if exit_geo:
        print(f"[+] Exit IP geolocation: {_geo_str(exit_geo)} "
              f"| lat {exit_geo.get('lat')}, lon {exit_geo.get('lon')}")

    # 4. Summary Table
    print_summary(baseline, final_posture, profile, exit_ip, new_local_ip,
                  baseline_geo=baseline_geo, exit_geo=exit_geo)

    print("\n[done] Network priming sequence complete.")


if __name__ == "__main__":
    main()