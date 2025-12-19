#!/usr/bin/env python3
import argparse, json, os, platform, shutil, subprocess, time

def sh(cmd):
    return subprocess.run(cmd, shell=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

def has(cmd):
    return shutil.which(cmd) is not None

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--disk-threshold", type=int, default=int(os.getenv("DISK_THRESHOLD", "85")))
    p.add_argument("--ping", default=os.getenv("PING_TARGET", "8.8.8.8"))
    p.add_argument("--services", default=os.getenv("CHECK_SERVICES", "ssh systemd-journald cron"))
    p.add_argument("--format", choices=["json","text"], default=os.getenv("FORMAT","json"))
    args = p.parse_args()

    failures = []
    # Basics
    host = platform.node()
    kernel = platform.release()
    os_name = ""
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    os_name = line.split("=",1)[1].strip().strip('"')
                    break
    except:
        pass

    uptime_out = sh("uptime -p").stdout.strip()
    boot_out = sh("who -b | awk '{print $3\" \"$4}'").stdout.strip()
    with open("/proc/loadavg") as f:
        l1,l5,l15,_rest = f.read().split()[:4]

    # CPU usage (quick/approx)
    cpu_usage = None
    if has("mpstat"):
        r = sh("mpstat 1 1 | awk '/Average/ && $3 ~ /CPU/ {next} /Average/ {print 100 - $NF}'")
        cpu_usage = r.stdout.strip() or None
    else:
        r = sh("top -bn1 | awk -F'[, ]+' '/%Cpu/ {print 100-$8}'")
        cpu_usage = r.stdout.strip() or None

    # Memory
    def read_mem_kb(key):
        r = sh(f"grep {key} /proc/meminfo | awk '{{print $2}}'")
        return int(r.stdout.strip() or 0)
    mem_total = read_mem_kb("MemTotal")
    mem_avail = read_mem_kb("MemAvailable")
    mem_used = mem_total - mem_avail
    mem_used_pct = round((mem_used / mem_total) * 100, 1) if mem_total else 0

    # Disk
    r = sh("df -hP / | awk 'NR==2{print $1\" \"$2\" \"$3\" \"$4\" \"$5\" \"$6}'")
    disk_fs, disk_size, disk_used, disk_avail, disk_use_pct, disk_mount = (r.stdout.strip().split() if r.stdout.strip() else ["","","","","",""])
    disk_use_num = int(disk_use_pct.rstrip("%")) if disk_use_pct else 0
    if disk_use_num > args.disk_threshold:
        failures.append(f"Root disk usage {disk_use_pct} exceeds {args.disk_threshold}%")

    # Ping
    ping_ok = "unknown"
    if has("ping"):
        ping_ok = "true" if sh(f"ping -c 1 -W 2 {args.ping}").returncode == 0 else "false"
        if ping_ok == "false":
            failures.append(f"Ping to {args.ping} failed")

    # Services
    services_down = []
    if has("systemctl"):
        for svc in args.services.split():
            if sh(f"systemctl is-active --quiet {svc}").returncode != 0:
                services_down.append(svc)
        if services_down:
            failures.append("Services not active: " + " ".join(services_down))

    # Upgrades
    pkg_upgrades = None
    if has("apt-get"):
        r = sh("apt-get -s upgrade | awk '/^Inst /{count++} END{print count+0}'")
        try:
            pkg_upgrades = int((r.stdout or "0").strip())
        except:
            pkg_upgrades = None

    # Docker
    docker = None
    if has("docker"):
        docker = "running" if sh("docker info").returncode == 0 else "installed_not_running_or_no_perms"

    status = "healthy" if not failures else "unhealthy"

    data = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "host": host,
        "os": os_name,
        "kernel": kernel,
        "uptime": uptime_out,
        "boot_time": boot_out,
        "loadavg": {"1m": l1, "5m": l5, "15m": l15},
        "cpu_usage_pct": cpu_usage,
        "memory": {"used_pct": mem_used_pct, "used_kb": mem_used, "total_kb": mem_total},
        "disk_root": {"used_pct": disk_use_pct, "size": disk_size, "avail": disk_avail, "threshold_pct": args.disk_threshold},
        "network": {"ping_host": args.ping, "ping_ok": ping_ok},
        "services": {"checked": args.services.split(), "down": services_down},
        "packages": {"upgrades_pending": pkg_upgrades},
        "docker": docker,
        "status": status,
        "failures": failures
    }

    if args.format == "json":
        print(json.dumps(data, indent=2))
    else:
        print(f"Ubuntu Health Check - {data['timestamp']}")
        print(f"Status: {status}")
        print(json.dumps(data, indent=2))

    exit(0 if status == "healthy" else 1)

if __name__ == "__main__":
    main()
