with open('../hermes_home/scripts/hibernation_monitor.py', 'r', encoding='utf-8') as f:
    text = f.read()

patch = '''        with open('../logs/hibernation_events.log', 'a') as f:
            f.write(f'HIBERNATION DETECTED at {time.ctime()}. delta: {current_time - last_time}\\\\n')'''

replacement = '''        with open('../logs/hibernation_events.log', 'a') as f:
            f.write(f'HIBERNATION DETECTED at {time.ctime()}. delta: {current_time - last_time}\\\\n')
        
        # ACTIVATE KILL SWITCH VIA STATE FILE
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

text = text.replace(patch, replacement)

with open('../hermes_home/scripts/hibernation_monitor.py', 'w', encoding='utf-8') as f:
    f.write(text)
