import json
import os
import uuid
from datetime import datetime, timezone

jobs_path = '../hermes_home/cron/jobs.json'

with open(jobs_path, 'r', encoding='utf-8') as f:
    data = json.load(f)

# Arbitrage job 4h
arbitrage_job = {
    "id": str(uuid.uuid4())[:12],
    "name": "doge-arbitrage-scout-4h",
    "prompt": "doge-arbitrage-scout-4h",
    "skills": [],
    "skill": None,
    "model": None,
    "provider": None,
    "base_url": None,
    "script": "doge_arbitrage_scout.py",
    "no_agent": True,
    "context_from": None,
    "schedule": {
    "kind": "cron",
    "expr": "0 */4 * * *",
    "display": "0 */4 * * *"
    },
    "schedule_display": "0 */4 * * *",
    "repeat": {
    "times": None,
    "completed": 0
    },
    "enabled": True,
    "state": "scheduled",
    "paused_at": None,
    "paused_reason": None,
    "created_at": datetime.now(timezone.utc).isoformat(),
    "next_run_at": datetime.now(timezone.utc).isoformat(), 
    "last_run_at": None,
    "last_status": None,
    "last_error": None,
    "last_delivery_error": None,
    "deliver": "whatsapp",
    "origin": None,
    "enabled_toolsets": None,
    "workdir": None
}

# Grid job 1h
grid_job = {
    "id": str(uuid.uuid4())[:12],
    "name": "doge-grid-scout-1h",
    "prompt": "doge-grid-scout-1h",
    "skills": [],
    "skill": None,
    "model": None,
    "provider": None,
    "base_url": None,
    "script": "doge_grid_scout.py",
    "no_agent": True,
    "context_from": None,
    "schedule": {
    "kind": "cron",
    "expr": "0 * * * *",
    "display": "0 * * * *"
    },
    "schedule_display": "0 * * * *",
    "repeat": {
    "times": None,
    "completed": 0
    },
    "enabled": True,
    "state": "scheduled",
    "paused_at": None,
    "paused_reason": None,
    "created_at": datetime.now(timezone.utc).isoformat(),
    "next_run_at": datetime.now(timezone.utc).isoformat(), 
    "last_run_at": None,
    "last_status": None,
    "last_error": None,
    "last_delivery_error": None,
    "deliver": "whatsapp",
    "origin": None,
    "enabled_toolsets": None,
    "workdir": None
}

existing_names = [j['name'] for j in data['jobs']]
if "doge-arbitrage-scout-4h" not in existing_names:
    data['jobs'].append(arbitrage_job)
if "doge-grid-scout-1h" not in existing_names:
    data['jobs'].append(grid_job)

data["updated_at"] = datetime.now(timezone.utc).isoformat()

with open(jobs_path, 'w', encoding='utf-8') as f:
    json.dump(data, f, indent=2)

print("Cron jobs updated correctly!")
