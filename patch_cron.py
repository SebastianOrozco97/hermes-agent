import json

with open('../hermes_home/cron/jobs.json', 'r') as f:
    data = json.load(f)

for job in data['jobs']:
    if 'doge-arbitrage-scout' in job['name']:
        job['schedule']['expr'] = '*/7 * * * *'
        job['schedule']['display'] = '*/7 * * * *'
        job['schedule_display'] = '*/7 * * * *'
    if 'doge-grid-scout' in job['name']:
        job['schedule']['expr'] = '*/7 * * * *'
        job['schedule']['display'] = '*/7 * * * *'
        job['schedule_display'] = '*/7 * * * *'

with open('../hermes_home/cron/jobs.json', 'w') as f:
    json.dump(data, f, indent=2)
