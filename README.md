Ubuntu Health Check Demo (PagerDuty Runbook Automation + GitHub)

This repo contains:
- A portable Ubuntu health check (bash + optional Python).
- A Runbook Automation job definition synced via the Git SCM plugin.
- Ready for webhooks to auto-import job changes.

#### 1) Local test
```bash
chmod +x scripts/ubuntu_health_check.sh
./scripts/ubuntu_health_check.sh --markdown
./scripts/ubuntu_health_check.sh --disk-threshold 80 --ping 1.1.1.1 --format both
python3 scripts/ubuntu_health_check.py --format json
