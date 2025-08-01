import atexit
import os
import RPi.GPIO as GPIO
import subprocess
import sys
import time
import re
import signal
from threading import Thread, RLock, Event
from flask import Flask, render_template_string, jsonify

# ==============================================================================
# === GLOBAL CONFIGURATION & STATE
# ==============================================================================

# --- GPIO CONFIG ---
LANE_GPIO = {
    '81': {'R': 16, 'Y': 20, 'G': 21},
    '82': {'R': 5,  'Y': 6,  'G': 13},
    '83': {'R': 17, 'Y': 27, 'G': 22},
    '84': {'R': 10, 'Y': 9,  'G': 11},
}

# --- DYNAMIC "AUTOMATIC MODE" CONFIG ---
RTSP_URLS = {
    '81': "rtsp://admin:p2n123@192.168.29.81",
    '82': "rtsp://admin:p2n123@192.168.29.82",
    '83': "rtsp://admin:p2n123@192.168.29.83",
    '84': "rtsp://admin:p2n123@192.168.29.84"
}
# Path to the Hailo model runner script
HOME_DIR = os.path.expanduser('~')
CAR_SCRIPT_COMMAND = f"bash -c 'source {HOME_DIR}/hailo-rpi5-examples/setup_env.sh && python3 {HOME_DIR}/hailo-rpi5-examples/basic_pipelines/car.py --input {{url}} --hef-path {HOME_DIR}/hailo-rpi5-examples/resources/models/hailo8/yolov8m_traffic.hef'"


# --- FIXED "MANUAL MODE" CONFIG ---
MANUAL_LANE_TIMINGS = {'81': 45, '82': 45, '83': 25, '84': 25}
YELLOW_LIGHT_DURATION = 3

# --- SYSTEM STATE ---
app_lock = RLock()  # Re-entrant lock to prevent deadlocks
active_task = {"thread": None, "stop_event": None, "mode": "None"}
is_gpio_initialized = False
vehicle_counts = {}

# ==============================================================================
# === GPIO & SYSTEM INITIALIZATION
# ==============================================================================

def initialize_gpio():
    """Initializes GPIO pins and checks for necessary permissions."""
    global is_gpio_initialized
    if os.geteuid() != 0:
        print(f"\n[ERROR] GPIO access requires root. Please run with 'sudo'.\n")
        return False
    try:
        print("[INFO] Initializing GPIO...")
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        for pins in LANE_GPIO.values():
            for pin in pins.values():
                GPIO.setup(pin, GPIO.OUT)
                GPIO.output(pin, 0)
        is_gpio_initialized = True
        print("[SUCCESS] GPIO initialized successfully.")
        return True
    except Exception as e:
        print(f"[FATAL] Could not initialize GPIO. Reason: {e}")
        return False

def cleanup_gpio():
    print("ðŸ§¹ Cleaning up GPIO...")
    if is_gpio_initialized:
        for pins in LANE_GPIO.values():
            for pin in pins.values():
                GPIO.output(pin, 0)
        GPIO.cleanup()
        print("[INFO] GPIO cleanup complete.")

atexit.register(cleanup_gpio)

# ==============================================================================
# === TRAFFIC LIGHT LOGIC & HELPER FUNCTIONS
# ==============================================================================

def set_lights(lane_id, red, yellow, green):
    """Sets the R, Y, G lights for a specific lane."""
    pins = LANE_GPIO[lane_id]
    GPIO.output(pins['R'], red)
    GPIO.output(pins['Y'], yellow)
    GPIO.output(pins['G'], green)

def all_red_except(active_lane=None):
    """Sets all lanes to red, except for the specified active lane."""
    for lane_id in LANE_GPIO:
        if lane_id != active_lane:
            set_lights(lane_id, 1, 0, 0)

# --- Dynamic "Automatic Mode" Helpers ---

def parse_vehicle_line(line):
    """Parses a line of output from the car.py script to get total vehicles."""
    pattern = r"car:\s*(\d+)\s+bicycle:\s*(\d+)\s+motorcycle:\s*(\d+)\s+bus:\s*(\d+)\s+truck:\s*(\d+)"
    match = re.search(pattern, line)
    return sum(int(x) for x in match.groups()) if match else None

def run_car_script(ip_suffix, url):
    """Runs the vehicle counting script for a single stream."""
    command = CAR_SCRIPT_COMMAND.format(url=url)
    process = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, preexec_fn=os.setsid)
    count = 0
    try:
        start_time = time.time()
        while time.time() - start_time < 5: # Run for 5 seconds
            line = process.stdout.readline()
            if not line: break
            print(f"[{ip_suffix}] {line.strip()}")
            parsed = parse_vehicle_line(line)
            if parsed is not None: count = parsed
    finally:
        try: # Gracefully shut down the subprocess group
            os.killpg(os.getpgid(process.pid), signal.SIGINT)
            time.sleep(1)
            if process.poll() is None: os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            process.communicate(timeout=3)
        except Exception as e:
            print(f"[{ip_suffix}] Error during shutdown: {e}")
    print(f"âœ… IP {ip_suffix} final vehicle count: {count}")
    vehicle_counts[ip_suffix] = count

def calculate_timings():
    """Calculates green light durations based on vehicle counts."""
    sorted_lanes = sorted(vehicle_counts.items(), key=lambda x: x[1], reverse=True)
    # Ensure total_vehicles is at least 1 to avoid division by zero
    total_vehicles = sum(max(c, 1) for _, c in sorted_lanes) or 1
    total_green_time = 140 # Total available green time in a cycle
    
    lane_timings = {}
    for lane_id, count in sorted_lanes:
        weight = max(count, 1) / total_vehicles
        green_time = int(weight * total_green_time)
        green_time = max(10, min(green_time, 80)) # Clamp between 10s and 80s
        lane_timings[lane_id] = green_time
    return sorted_lanes, lane_timings

# ==============================================================================
# === BACKGROUND MODE CYCLES (RUN IN THREADS)
# ==============================================================================

def automatic_mode_cycle(stop_event: Event):
    """The main loop for the dynamic, density-based automatic mode."""
    try:
        print("[INFO] Automatic mode thread started.")
        while not stop_event.is_set():
            print("\nðŸš¦ AUTOMATIC: Evaluating traffic density...")
            for ip_suffix, url in RTSP_URLS.items():
                if stop_event.is_set(): break
                run_car_script(ip_suffix, url)
            if stop_event.is_set(): break

            print("\nðŸ”¢ AUTOMATIC: Final Vehicle Counts:")
            for lane_id in sorted(vehicle_counts):
                print(f"  Lane {lane_id}: {vehicle_counts.get(lane_id, 0)} vehicles")

            sorted_lanes, lane_timings = calculate_timings()
            print("\nâ±ï¸ AUTOMATIC: Calculated Green Times:")
            for lane_id, _ in sorted_lanes:
                print(f"  Lane {lane_id}: {lane_timings[lane_id]}s")
            
            print("\nðŸš¦ AUTOMATIC: Starting traffic light cycle...")
            for lane_id, _ in sorted_lanes:
                if stop_event.is_set(): break
                green_time = lane_timings[lane_id]

                print(f"ðŸŸ¢ AUTOMATIC: Lane {lane_id} GREEN for {green_time}s")
                all_red_except()
                set_lights(lane_id, 0, 0, 1)
                if stop_event.wait(green_time): break

                print(f"ðŸŸ¡ AUTOMATIC: Lane {lane_id} YELLOW for {YELLOW_LIGHT_DURATION}s")
                set_lights(lane_id, 0, 1, 0)
                if stop_event.wait(YELLOW_LIGHT_DURATION): break
                set_lights(lane_id, 1, 0, 0)

            if not stop_event.is_set():
                print("\nðŸ”„ AUTOMATIC: Cycle complete. Waiting 5s before next evaluation.")
                if stop_event.wait(5): break
    except Exception as e:
        print(f"\n\n[FATAL ERROR IN AUTOMATIC THREAD] ==> {e}\n\n")
    finally:
        print("[INFO] Automatic mode thread finished.")


def manual_traffic_cycle(stop_event: Event):
    """The main loop for the fixed-timing manual mode."""
    try:
        print("[INFO] Manual mode thread started.")
        while not stop_event.is_set():
            for lane_id in ['81', '82', '83', '84']:
                if stop_event.is_set(): break
                green_time = MANUAL_LANE_TIMINGS[lane_id]
                print(f"ðŸŸ¢ MANUAL: Lane {lane_id} GREEN for {green_time}s")
                all_red_except()
                set_lights(lane_id, 0, 0, 1)
                if stop_event.wait(green_time): break

                print(f"ðŸŸ¡ MANUAL: Lane {lane_id} YELLOW for {YELLOW_LIGHT_DURATION}s")
                set_lights(lane_id, 0, 1, 0)
                if stop_event.wait(YELLOW_LIGHT_DURATION): break
                set_lights(lane_id, 1, 0, 0)
    except Exception as e:
        print(f"\n\n[FATAL ERROR IN MANUAL THREAD] ==> {e}\n\n")
    finally:
        print("[INFO] Manual mode thread finished.")

def yellow_light_cycle(stop_event: Event):
    """The main loop for the blinking yellow lights mode."""
    try:
        print("[INFO] Yellow mode thread started.")
        while not stop_event.is_set():
            all_red_except() # Turn all R and G off
            print("ðŸŸ¡ YELLOW MODE: Lights ON")
            for lane_id in LANE_GPIO: set_lights(lane_id, 0, 1, 0)
            if stop_event.wait(2): break

            print("âš« YELLOW MODE: Lights OFF")
            for lane_id in LANE_GPIO: set_lights(lane_id, 0, 0, 0)
            if stop_event.wait(2): break
    except Exception as e:
        print(f"\n\n[FATAL ERROR IN YELLOW THREAD] ==> {e}\n\n")
    finally:
        print("[INFO] Yellow mode thread finished.")

# ==============================================================================
# === FLASK WEB SERVER & MODE CONTROLLER
# ==============================================================================

app = Flask(__name__)

def stop_current_task():
    """Stops any running background task. Safe to call from a locked context."""
    with app_lock:
        if active_task["thread"]:
            print(f"[INFO] Stopping {active_task['mode']} thread...")
            active_task["stop_event"].set()
            active_task["thread"].join() # Wait for the thread to finish
            active_task["thread"] = None
        if active_task["mode"] != "None":
            active_task["mode"] = "None"
            print("[INFO] All tasks stopped. Setting lights to safe state (all off).")
            for lane_id in LANE_GPIO: set_lights(lane_id, 0, 0, 0)

@app.route('/')
def index():
    return render_template_string("""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Traffic Controller</title><style>body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;background-color:#282c34;color:white;display:flex;justify-content:center;align-items:center;height:100vh;margin:0}.container{text-align:center;background:#3c4049;padding:40px;border-radius:15px;box-shadow:0 10px 25px rgba(0,0,0,.5)}h1{margin-top:0}.buttons{margin-top:20px;display:flex;flex-wrap:wrap;gap:15px;justify-content:center}button{padding:15px 30px;font-size:16px;cursor:pointer;border:none;border-radius:8px;color:white;font-weight:700;transition:all .2s ease}#btn-auto{background-color:#0d6efd}#btn-manual{background-color:#198754}#btn-yellow{background-color:#ffc107}button:hover{filter:brightness(1.15);transform:translateY(-2px)}.status{margin-top:30px;font-size:1.2em}.status-box{background-color:#282c34;padding:12px 22px;border-radius:8px;display:inline-block}.status-box span{font-weight:700;color:#61dafb;text-transform:uppercase}</style></head><body><div class="container"><h1>Traffic Control System</h1><div class="buttons"><button id="btn-auto" onclick="setMode('Automatic')">Automatic Mode</button><button id="btn-manual" onclick="setMode('Manual')">Manual Mode</button><button id="btn-yellow" onclick="setMode('Yellow')">Yellow Mode</button></div><div class="status"><div class="status-box">Current Mode: <span id="current-mode">Fetching...</span></div></div></div><script>const currentModeSpan=document.getElementById("current-mode");let currentMode="None";function setMode(e){if(e===currentMode)return void alert("This mode is already running.");if(confirm(`Are you sure you want to switch to ${e} mode?`)){const o="/set_mode/"+e;console.log(`Sending request to: ${o}`),fetch(o,{method:"POST"}).then(e=>{if(!e.ok)throw new Error(`Server error: ${e.status}`);return e.json()}).then(e=>{console.log("Server response:",e.message),updateStatus()}).catch(e=>{console.error("Fetch Error:",e),alert("An error occurred. Check browser console (F12).")})}}function updateStatus(){fetch("/status").then(e=>e.json()).then(e=>{currentMode=e.mode,currentModeSpan.textContent=e.mode}).catch(e=>console.error("Could not fetch status:",e))}setInterval(updateStatus,3e3),document.addEventListener("DOMContentLoaded",updateStatus);</script></body></html>""")

@app.route('/status')
def status():
    return jsonify({"mode": active_task["mode"]})

@app.route('/set_mode/<mode>', methods=['POST'])
def set_mode(mode):
    print(f"\n[REQUEST] Received request to set mode to: {mode}")
    with app_lock:
        if active_task["mode"] == mode:
            return jsonify({"message": f"{mode} mode is already running."}), 400

        stop_current_task()
        
        target_map = {
            'Automatic': automatic_mode_cycle,
            'Manual': manual_traffic_cycle,
            'Yellow': yellow_light_cycle
        }
        
        target_func = target_map.get(mode)
        if not target_func:
            print(f"[ERROR] Invalid mode '{mode}' specified.")
            return jsonify({"message": "Invalid mode specified."}), 400

        print(f"[INFO] Attempting to start {mode} mode...")
        stop_event = Event()
        thread = Thread(target=target_func, args=(stop_event,))
        active_task.update({"thread": thread, "stop_event": stop_event, "mode": mode})
        thread.start()
        print(f"[SUCCESS] {mode} mode thread started.")
        return jsonify({"message": f"{mode} mode started."})

# ==============================================================================
# === MAIN EXECUTION
# ==============================================================================
if __name__ == '__main__':
    if not initialize_gpio():
        sys.exit(1)
    print("\n[INFO] Starting Flask server on http://0.0.0.0:5000")
    print("      Open this address in a browser on any device in the same network.")
    app.run(host='0.0.0.0', port=5000, threaded=True)
