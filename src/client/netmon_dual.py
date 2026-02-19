#!/usr/bin/env python3
import azure
import csv
import os
import subprocess
import time
import configparser
from datetime import datetime, date
from pathlib import Path
from azure.storage.blob import BlobServiceClient
from configparser import ConfigParser

# ----------------------------
# Settings
# ----------------------------
INTERVAL_SECONDS = 30
LOG_DIR = Path("./log")  # change if you prefer
IFACES = ["eth0", "wlan0"]

PUBLIC_IP_URL = "https://api.ipify.org"

inifileHandler = ConfigParser()
URL2CHECK = inifileHandler.get('global', 'url2check')
AZURE_CONTAINER = inifileHandler.get('Azure', 'Container')
AZURE_STORAGE_CONNECTION_STRING = inifileHandler.get('Azure', 'ConnectionString')

def run(cmd, timeout=8):
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)


def ensure_log_dir():
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        return LOG_DIR
    except PermissionError:
        fallback = Path.home() / "netmon_logs"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def log_path_for_today(log_dir: Path):
    return log_dir / f"netmon_{date.today().isoformat()}.csv"


def append_row(csv_path: Path, row: dict):
    new_file = not csv_path.exists()
    with csv_path.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(row.keys()))
        if new_file:
            w.writeheader()
        w.writerow(row)


def iface_is_up(iface: str) -> bool:
    try:
        p = run(["ip", "link", "show", "dev", iface], timeout=2)
        if p.returncode != 0:
            return False
        # look for "state UP"
        return "state UP" in p.stdout
    except Exception:
        return False


def iface_ipv4(iface: str) -> str:
    """
    Returns IPv4 address (string) or "" if none.
    """
    try:
        p = run(["ip", "-4", "addr", "show", "dev", iface], timeout=2)
        if p.returncode != 0:
            return ""
        # inet 192.168.1.23/24 ...
        for line in p.stdout.splitlines():
            line = line.strip()
            if line.startswith("inet "):
                return line.split()[1].split("/")[0]
        return ""
    except Exception:
        return ""


def gateway_for_iface(iface: str) -> str:
    """
    Returns default gateway IP for a specific iface, or "".
    """
    try:
        p = run(["ip", "route", "show", "default", "dev", iface], timeout=2)
        if p.returncode != 0:
            return ""
        # default via 192.168.1.1 dev eth0 ...
        parts = p.stdout.strip().split()
        for i, tok in enumerate(parts):
            if tok == "via" and i + 1 < len(parts):
                return parts[i + 1]
        return ""
    except Exception:
        return ""


def ping_via_iface(iface: str, host: str, timeout_s=1):
    """
    Returns (ok: bool, rtt_ms: float|None)
    """
    try:
        p = run(["ping", "-I", iface, "-c", "1", "-W", str(timeout_s), host], timeout=timeout_s + 2)
        if p.returncode != 0:
            return False, None
        rtt = None
        for line in p.stdout.splitlines():
            if "time=" in line:
                # ... time=7.23 ms
                try:
                    rtt_str = line.split("time=")[1].split()[0]
                    rtt = float(rtt_str)
                except Exception:
                    pass
                break
        return True, rtt
    except Exception:
        return False, None


def curl_head_via_iface(iface: str, url: str, timeout_s=6):
    """
    Returns (ok: bool, response_ms: float|None, http_code: int|None)
    Uses curl with --interface to force the egress interface.
    """
    try:
        # time_total in seconds; convert to ms
        cmd = [
            "curl", "--interface", iface,
            "-I",  # HEAD
            "-L",  # follow redirects
            "--max-time", str(timeout_s),
            "-o", "/dev/null",
            "-sS",
            "-w", "%{http_code} %{time_total}",
            url
        ]
        p = run(cmd, timeout=timeout_s + 2)
        if p.returncode != 0:
            return False, None, None
        out = p.stdout.strip().split()
        if len(out) != 2:
            return False, None, None
        code = int(out[0])
        t_sec = float(out[1])
        ms = t_sec * 1000.0
        ok = 200 <= code < 600  # treat any HTTP response as "reachable"
        return ok, ms, code
    except Exception:
        return False, None, None


def curl_get_via_iface(iface: str, url: str, timeout_s=6):
    """
    Returns response body string or "".
    """
    try:
        cmd = [
            "curl", "--interface", iface,
            "-L",
            "--max-time", str(timeout_s),
            "-sS",
            url
        ]
        p = run(cmd, timeout=timeout_s + 2)
        if p.returncode != 0:
            return ""
        return p.stdout.strip()
    except Exception:
        return ""


def azure_upload(local_file: Path):
    """
    Uploads to Azure Blob Storage if env vars are present.
    """
    container = os.getenv("AZURE_CONTAINER", "").strip()
    if not container:
        return False, "AZURE_CONTAINER not set"

    conn_str = os.getenv("AZURE_STORAGE_CONNECTION_STRING", "").strip()
    sas = os.getenv("AZURE_STORAGE_SAS_TOKEN", "").strip()
    account = os.getenv("AZURE_STORAGE_ACCOUNT", AZURE_ACCOUNT_DEFAULT).strip()

    try:
        if conn_str:
            bsc = BlobServiceClient.from_connection_string(conn_str)
        elif sas:
            account_url = f"https://{account}.blob.core.windows.net"
            bsc = BlobServiceClient(account_url=account_url, credential=sas)
        else:
            return False, "No AZURE_STORAGE_CONNECTION_STRING or AZURE_STORAGE_SAS_TOKEN set"

        blob_name = local_file.name
        blob_client = bsc.get_blob_client(container=container, blob=blob_name)

        with local_file.open("rb") as data:
            blob_client.upload_blob(data, overwrite=True)

        return True, f"Uploaded to container '{container}' as '{blob_name}'"
    except Exception as e:
        return False, f"Azure upload failed: {e}"


def check_iface(iface: str):
    """
    Returns dict with results for one interface.
    """
    up = iface_is_up(iface)
    ip4 = iface_ipv4(iface) if up else ""
    gw = gateway_for_iface(iface) if ip4 else ""

    local_ok = False
    gw_rtt = None
    if gw:
        local_ok, gw_rtt = ping_via_iface(iface, gw, timeout_s=1)

    nos_ok = False
    nos_ms = None
    nos_code = None
    public_ip = ""

    if local_ok:
        nos_ok, nos_ms, nos_code = curl_head_via_iface(iface, URL2CHECK, timeout_s=6)
        if nos_ok:
            public_ip = curl_get_via_iface(iface, PUBLIC_IP_URL, timeout_s=6)

    iface_ok = bool(local_ok and nos_ok)

    return {
        "iface_up": int(up),
        "iface_ipv4": ip4,
        "gateway": gw,
        "local_ok": int(local_ok),
        "gateway_rtt_ms": f"{gw_rtt:.2f}" if gw_rtt is not None else "",
        "nos_ok": int(nos_ok),
        "nos_status": nos_code if nos_code is not None else "",
        "nos_response_ms": f"{nos_ms:.2f}" if nos_ms is not None else "",
        "public_ip": public_ip,
        "iface_ok": int(iface_ok),
    }


def main():
    log_dir = ensure_log_dir()

    while True:
        ts = datetime.now().isoformat(timespec="seconds")
        results = {iface: check_iface(iface) for iface in IFACES}

        # overall OK if any iface works
        overall_ok = any(results[i]["iface_ok"] == 1 for i in IFACES)

        # choose preferred interface for upload (wired first)
        preferred = "eth0" if results.get("eth0", {}).get("iface_ok") == 1 else (
            "wlan0" if results.get("wlan0", {}).get("iface_ok") == 1 else ""
        )

        # Flatten into one CSV row with per-iface columns
        row = {
            "timestamp": ts,
            "overall_ok": int(overall_ok),
            "preferred_iface": preferred,
        }

        for iface in IFACES:
            r = results[iface]
            for k, v in r.items():
                row[f"{iface}_{k}"] = v

        csv_path = log_path_for_today(log_dir)
        print(f"{ts} - add results to {csv_path}")
        append_row(csv_path, row)

        # Upload the daily file if at least one interface is OK
        if overall_ok:
            ok, msg = azure_upload(csv_path)
            try:
                with (log_dir / "netmon_upload.log").open("a") as f:
                    f.write(f"{ts} | {csv_path.name} | overall_ok={overall_ok} | preferred={preferred} | {ok} | {msg}\n")
            except Exception:
                pass

        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
