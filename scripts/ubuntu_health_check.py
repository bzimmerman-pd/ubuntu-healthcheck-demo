#!/usr/bin/env python3
"""Ubuntu host health check.

Exit codes: 0 = healthy, 1 = unhealthy, 2 = check itself failed to run.
Runs standalone (python3 ubuntu_healthcheck.py ...), as a Rundeck
Runbook Automation script step (scriptInterpreter: python3), or as a
GitHub Actions step (writes to $GITHUB_OUTPUT / $GITHUB_STEP_SUMMARY
when present).
"""
import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from urllib.parse import urlparse

SUBPROCESS_TIMEOUT = 10


def sh(cmd, timeout=SUBPROCESS_TIMEOUT):
    """Run a fixed, non-user-influenced shell pipeline."""
    try:
        return subprocess.run(
            cmd, shell=True, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, returncode=124, stdout="", stderr="timed out")


def run(argv, timeout=SUBPROCESS_TIMEOUT):
    """Run an argv list directly (no shell) so untrusted input can't inject commands."""
    try:
        return subprocess.run(
            argv, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return subprocess.CompletedProcess(argv, returncode=124, stdout="", stderr=str(e))


def has(cmd):
    return shutil.which(cmd) is not None


def read_os_name():
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return ""


def read_loadavg():
    try:
        with open("/proc/loadavg") as f:
            parts = f.read().split()
        return parts[0], parts[1], parts[2]
    except (OSError, IndexError):
        return "unknown", "unknown", "unknown"


def read_mem_kb(key):
    r = sh(f"grep {key} /proc/meminfo | awk '{{print $2}}'")
    try:
        return int(r.stdout.strip() or 0)
    except ValueError:
        return 0


def get_cpu_usage():
    if has("mpstat"):
        r = sh("mpstat 1 1 | awk '/Average/ && $3 ~ /CPU/ {next} /Average/ {print 100 - $NF}'")
    else:
        r = sh("top -bn1 | awk -F'[, ]+' '/%Cpu/ {print 100-$8}'")
    return r.stdout.strip() or None


def check_disk(threshold, failures):
    r = sh("df -hP / | awk 'NR==2{print $1\" \"$2\" \"$3\" \"$4\" \"$5\" \"$6}'")
    fields = r.stdout.strip().split() if r.stdout.strip() else ["", "", "", "", "", ""]
    disk_fs, disk_size, disk_used, disk_avail, disk_use_pct, disk_mount = fields
    try:
        disk_use_num = int(disk_use_pct.rstrip("%")) if disk_use_pct else 0
    except ValueError:
        disk_use_num = 0
    if disk_use_num > threshold:
        failures.append(f"Root disk usage {disk_use_pct} exceeds {threshold}%")
    return {"used_pct": disk_use_pct, "size": disk_size, "avail": disk_avail, "threshold_pct": threshold}


def ping_host(target):
    """Accept a bare hostname/IP or a full URL and return just the host part (ping doesn't take schemes/paths)."""
    parsed = urlparse(target if "//" in target else f"//{target}")
    return parsed.hostname or target


def check_ping(target, failures):
    if not has("ping"):
        return "unknown"
    host = ping_host(target)
    ok = run(["ping", "-c", "1", "-W", "2", host]).returncode == 0
    if not ok:
        failures.append(f"Ping to {host} failed")
    return "true" if ok else "false"


def check_services(services, failures):
    down = []
    if has("systemctl"):
        for svc in services:
            if run(["systemctl", "is-active", "--quiet", svc]).returncode != 0:
                down.append(svc)
        if down:
            failures.append("Services not active: " + " ".join(down))
    return down


def check_pkg_upgrades():
    if not has("apt-get"):
        return None
    r = sh("apt-get -s upgrade | awk '/^Inst /{count++} END{print count+0}'")
    try:
        return int((r.stdout or "0").strip())
    except ValueError:
        return None


def check_docker():
    if not has("docker"):
        return None
    return "running" if sh("docker info").returncode == 0 else "installed_not_running_or_no_perms"


def emit_ci_outputs(status, failures):
    gh_output = os.getenv("GITHUB_OUTPUT")
    if gh_output:
        with open(gh_output, "a") as f:
            f.write(f"status={status}\n")
            f.write(f"failure_count={len(failures)}\n")

    gh_summary = os.getenv("GITHUB_STEP_SUMMARY")
    if gh_summary:
        with open(gh_summary, "a") as f:
            f.write(f"### Ubuntu Health Check: {status}\n")
            if failures:
                f.write("\n".join(f"- {msg}" for msg in failures) + "\n")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--disk-threshold", type=int, default=int(os.getenv("DISK_THRESHOLD", "85")))
    p.add_argument("--ping", default=os.getenv("PING_TARGET", "http://ec2-107-22-64-51.compute-1.amazonaws.com/"))
    p.add_argument("--services", default=os.getenv("CHECK_SERVICES", "apache2"))
    p.add_argument("--format", choices=["json", "text"], default=os.getenv("FORMAT", "json"))
    args = p.parse_args()

    if platform.system() != "Linux":
        print(json.dumps({"status": "error", "error": "this check only supports Linux hosts"}))
        return 2

    failures = []
    l1, l5, l15 = read_loadavg()
    mem_total = read_mem_kb("MemTotal")
    mem_avail = read_mem_kb("MemAvailable")
    mem_used = mem_total - mem_avail
    mem_used_pct = round((mem_used / mem_total) * 100, 1) if mem_total else 0

    data = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "host": platform.node(),
        "os": read_os_name(),
        "kernel": platform.release(),
        "uptime": sh("uptime -p").stdout.strip(),
        "boot_time": sh("who -b | awk '{print $3\" \"$4}'").stdout.strip(),
        "loadavg": {"1m": l1, "5m": l5, "15m": l15},
        "cpu_usage_pct": get_cpu_usage(),
        "memory": {"used_pct": mem_used_pct, "used_kb": mem_used, "total_kb": mem_total},
        "disk_root": check_disk(args.disk_threshold, failures),
        "network": {"ping_host": ping_host(args.ping), "ping_ok": check_ping(args.ping, failures)},
        "services": {"checked": args.services.split(), "down": check_services(args.services.split(), failures)},
        "packages": {"upgrades_pending": check_pkg_upgrades()},
        "docker": check_docker(),
    }
    data["status"] = "healthy" if not failures else "unhealthy"
    data["failures"] = failures

    if args.format == "json":
        print(json.dumps(data, indent=2))
    else:
        print(f"Ubuntu Health Check - {data['timestamp']}")
        print(f"Status: {data['status']}")
        print(json.dumps(data, indent=2))
        # bare status line for Rundeck's key-value-data log filter (regex ^(healthy|unhealthy)$)
        print(data["status"])

    emit_ci_outputs(data["status"], failures)
    return 0 if data["status"] == "healthy" else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(json.dumps({"status": "error", "error": str(e)}))
        sys.exit(2)
