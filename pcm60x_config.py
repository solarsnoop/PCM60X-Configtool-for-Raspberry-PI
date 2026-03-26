import os, time, serial, questionary

# --- 1. CRC & FIXING (PCM60X & Axpert) ---
def pcm60x_crc(cmd):
    result = 0
    for char in cmd:
        result ^= (ord(char) << 8)
        for _ in range(8):
            if (result << 1) & 0x10000: result = (result << 1) ^ 0x1021
            else: result <<= 1
            result &= 0xFFFF
    high, low = (result >> 8) & 0xFF, result & 0xFF
    def php_fix(b): return b + 1 if b in [0x0D, 0x0A, 0x28] else b
    return bytes([php_fix(high), php_fix(low)])

def axpert_crc(cmd):
    crc = 0
    for byte in cmd.encode('ascii'):
        crc ^= (byte << 8)
        for _ in range(8):
            if crc & 0x8000: crc = (crc << 1) ^ 0x1021
            else: crc <<= 1
            crc &= 0xFFFF
    return crc.to_bytes(2, byteorder='big')

# --- 2. HARDWARE SCAN ---
def scan_hardware():
    path = "/dev/serial/by-id/"
    if not os.path.exists(path): return {}
    devs = [os.path.join(path, f) for f in os.listdir(path) if "usb-Prolific" in f]
    found = {}
    print("\nScanning hardware profiles...")
    for d in devs:
        try:
            with serial.Serial(d, 2400, timeout=1.5) as ser:
                ser.write("QPIRI".encode('ascii') + pcm60x_crc("QPIRI") + b'\x0d')
                time.sleep(0.6)
                res = ser.read(100).decode('ascii', 'ignore')
                if '(' in res:
                    parts = res.replace('(', '').split()
                    if len(parts) > 2:
                        amp = float(parts[2])
                        found[d] = "PCM60X" if amp <= 60 else "Axpert/PIP"
                        print(f"IS: {os.path.basename(d)} -> {found[d]}")
        except: continue
    return found

# --- 3. LIVE DATA PARSING (Based on pcxcall1.py) ---
def get_live_data(ser, profile):
    # QPIGS command for live data
    ser.write(b"\x51\x50\x49\x47\x53\xB7\xA9\x0D") 
    time.sleep(0.2)
    res = ser.read(100)
    if not res.startswith(b'(') or len(res) < 50: return None
    
    try:
        if profile == "PCM60X":
            # PCM60X Slices from pcxcall1.py
            return {
                "PV V": f"{res[1:6].decode().strip()}V",
                "Batt V": f"{res[7:12].decode().strip()}V",
                "Chg A": f"{res[14:19].decode().strip()}A",
                "Watt": f"{res[31:35].decode().strip()}W"
            }
        else:
            # Axpert Slices for Device 6 from pcxcall1.py
            v_batt = float(res[41:46].decode().strip())
            a_chg = float(res[47:50].decode().strip())
            return {
                "PV V": f"{res[65:68].decode().strip()}V",
                "Batt V": f"{v_batt}V",
                "Chg A": f"{a_chg}A",
                "Watt": f"{v_batt * a_chg:.1f}W"
            }
    except: return None

def parse_settings(raw, profile):
    try:
        clean = raw.decode('ascii', 'ignore').replace('(', '').split()
        if profile == "PCM60X":
            sys_v = int(clean[1])
            f = sys_v // 12
            return {"sys_v": sys_v, "f": f, "amp": float(clean[2]), "bulk": float(clean[3])*f, "float": float(clean[4])*f}
        else:
            return {"sys_v": 24, "f": 1, "amp": float(clean[14]), "bulk": float(clean[10]), "float": float(clean[11])}
    except: return None

# --- 4. MAIN ---
def main():
    cell_input = questionary.text("Enter number of cells:", default="16").ask()
    cells = int(cell_input) if cell_input else 16
    profiles = scan_hardware()

    while True:
        choices = [questionary.Choice(title=f"{os.path.basename(k)} [{v}]", value=k) for k, v in profiles.items()]
        choices += [questionary.Separator(), "Rescan", "Exit"]
        dev_path = questionary.select("Select Controller:", choices=choices).ask()
        if dev_path in [None, "Exit"]: break
        if dev_path == "Rescan": profiles = scan_hardware(); continue
        
        current_profile = profiles[dev_path]
        try:
            with serial.Serial(dev_path, 2400, timeout=3) as ser:
                while True:
                    # Fetch Settings (QPIRI)
                    crc_func = pcm60x_crc if current_profile == "PCM60X" else axpert_crc
                    ser.write("QPIRI".encode('ascii') + crc_func("QPIRI") + b'\x0d')
                    time.sleep(0.5)
                    settings = parse_settings(ser.read(200), current_profile)
                    
                    # Fetch Live Data (QPIGS)
                    live = get_live_data(ser, current_profile)

                    print("\n" + "="*60)
                    print(f"   DEVICE: {os.path.basename(dev_path)} [{current_profile}]")
                    if live:
                        print(f"   LIVE:   {live['Watt']} | PV: {live['PV V']} | Batt: {live['Batt V']} | {live['Chg A']}")
                    print("-" * 60)
                    if settings:
                        print(f"   Setup Max Current: {settings['amp']} A")
                        print(f"   Bulk Voltage:      {settings['bulk']:.2f} V ({settings['bulk']/cells:.3f} V/Cell)")
                        print(f"   Float Voltage:     {settings['float']:.2f} V ({settings['float']/cells:.3f} V/Cell)")
                    print("="*60 + "\n")

                    act_choices = ["Max Current", "Bulk Voltage", "Float Voltage", "Refresh", "Switch Device", "Exit"]
                    action = questionary.select("Action:", choices=act_choices).ask()
                    if action == "Exit": return
                    if action in ["Refresh", "Switch Device"]: break

                    val_in = questionary.text("New Value:").ask()
                    if val_in:
                        try:
                            new_val = float(val_in)
                            cmd = ""
                            if current_profile == "PCM60X":
                                if "Current" in action: cmd = f"MCHGC0{int(new_val):02d}"
                                elif "Bulk" in action: cmd = f"PBAV{new_val/settings['f']:.2f}"
                                elif "Float" in action: cmd = f"PBFV{new_val/settings['f']:.2f}"
                            else:
                                if "Current" in action: cmd = f"MNCHGC{int(new_val):03d}"
                                elif "Bulk" in action: cmd = f"PCVV{new_val:.1f}"
                                elif "Float" in action: cmd = f"PBFT{new_val:.1f}"

                            if cmd and questionary.confirm(f"Send {cmd}?").ask():
                                ser.write(cmd.encode('ascii') + crc_func(cmd) + b'\x0d')
                                time.sleep(2)
                                print(f"Response: {ser.read(100).decode('ascii', 'ignore').strip()}")
                                time.sleep(2)
                        except: print("Invalid Input.")
        except Exception as e:
            print(f"Error: {e}"); time.sleep(2)

if __name__ == "__main__":
    main()
