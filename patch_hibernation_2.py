with open('../hermes_home/scripts/hibernation_monitor.py', 'r', encoding='utf-8') as f:
    text = f.read()

patch = '''        # ACTIVATE KILL SWITCH VIA STATE FILE
        state_file = '../hermes_home/gateway_state.json'
        if os.path.exists(state_file):
            try:
                with open(state_file, 'r') as sf:
                    gate_data = json.load(sf)
                gate_data['kill_switch_active'] = True
                gate_data['kill_switch_reason'] = "HIBERNATION / THERMAL FALL DETECTED"
                with open(state_file, 'w') as sf:
                    json.dump(gate_data, sf, indent=2)
                print("KILL SWITCH ENGAGED IN JSON STATE")
            except Exception as e:
                print("FAILED TO ENGAGE KILL SWITCH:", e)'''

replacement = '''        # ACTIVATE KILL SWITCH VIA GUARDRAILS
        kill_switch_path = os.path.expanduser('~/.hermes/binance-kill-switch')
        try:
            os.makedirs(os.path.dirname(kill_switch_path), exist_ok=True)
            with open(kill_switch_path, 'w') as ks:
                ks.write(json.dumps({
                    "active": True,
                    "reason": "HIBERNATION / THERMAL FALL DETECTED",
                    "timestamp": time.time()
                }))
            print("KILL SWITCH FIRED SUCCESSFULLY! HARD BLOCK ENGAGED.")
        except Exception as e:
            print("FAILED TO FIRE KILL SWITCH:", e)'''

text = text.replace(patch, replacement)

with open('../hermes_home/scripts/hibernation_monitor.py', 'w', encoding='utf-8') as f:
    f.write(text)
