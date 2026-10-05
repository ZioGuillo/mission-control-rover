"""
Prometheus metrics for RoboControl.


All metric objects are defined here.  Other modules import what they need.
The /metrics HTTP endpoint is registered in app/main.py.

Scrape config example (prometheus.yml):
  scrape_configs:
    - job_name: robocar
      static_configs:
        - targets: ['<pi-ip>:8000']
      metrics_path: /metrics
"""
from prometheus_client import Counter, Gauge, Histogram, Info

# ── Build info ───────────────────────────────────────────────────────
BUILD_INFO = Info("robocar_build", "RoboControl build information")
BUILD_INFO.info({"version": "2.0", "hardware": "rpi4"})

# ── System ───────────────────────────────────────────────────────────
CPU_PERCENT      = Gauge("robocar_cpu_percent",
                          "CPU usage percent (0-100)")
CPU_TEMP_CELSIUS = Gauge("robocar_cpu_temperature_celsius",
                          "CPU temperature in °C (0 if not available — Pi only)")
RAM_USED_BYTES   = Gauge("robocar_ram_used_bytes",   "RAM used in bytes")
RAM_TOTAL_BYTES  = Gauge("robocar_ram_total_bytes",  "Total RAM in bytes")
DISK_USED_BYTES  = Gauge("robocar_disk_used_bytes",  "Disk used in bytes (/)")
DISK_TOTAL_BYTES = Gauge("robocar_disk_total_bytes", "Disk total in bytes (/)")
UPTIME_SECONDS   = Gauge("robocar_uptime_seconds",   "Server uptime in seconds")

# ── Hardware availability ────────────────────────────────────────────
# Labels: component = motors | camera | servo
HW_AVAILABLE = Gauge("robocar_hardware_available",
                      "Hardware component availability (1=up 0=down)",
                      ["component"])

# ── Camera ───────────────────────────────────────────────────────────
CAMERA_FRAMES_TOTAL = Counter("robocar_camera_frames_total",
                               "Total MJPEG frames captured by the camera driver")
_cam_frame_last: int = 0  # last driver counter value seen — tracks delta

# ── Motor / movement ─────────────────────────────────────────────────
# Labels: action = forward | reverse | left | right | stop
MOTOR_CMDS_TOTAL    = Counter("robocar_motor_commands_total",
                               "Motor commands dispatched", ["action"])
MOTOR_BLOCKED_TOTAL = Counter("robocar_motor_blocked_total",
                               "Motor forward commands blocked by obstacle detection")
DISTANCE_METERS     = Gauge("robocar_estimated_distance_meters",
                             "Estimated distance driven this session in meters")

# ── Sensor ───────────────────────────────────────────────────────────
SONAR_DISTANCE_CM = Gauge("robocar_sonar_distance_cm",
                           "Last ultrasonic sensor reading in cm (0 = no reading yet)")
OBSTACLES_TOTAL   = Counter("robocar_obstacles_total",
                             "Total obstacle detections that triggered auto-stop")

# ── HTTP server ──────────────────────────────────────────────────────
# path label is normalised (see _norm_path in main.py) to avoid cardinality explosion
HTTP_REQUESTS_TOTAL = Counter("robocar_http_requests_total",
                               "HTTP requests handled",
                               ["method", "path", "status"])
HTTP_DURATION = Histogram("robocar_http_request_duration_seconds",
                           "HTTP request processing time",
                           ["path"],
                           buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5])

# ── Auth / security ──────────────────────────────────────────────────
# Labels: result = success | failure
LOGIN_ATTEMPTS_TOTAL  = Counter("robocar_login_attempts_total",
                                 "Login attempts by result", ["result"])
# Labels: endpoint = login | motors
RATE_LIMIT_HITS_TOTAL = Counter("robocar_rate_limit_hits_total",
                                 "Requests rejected by rate limiter", ["endpoint"])
ACTIVE_SESSIONS       = Gauge("robocar_active_sessions_total",
                               "Currently active (non-expired) user sessions")


# ── Helpers ──────────────────────────────────────────────────────────

def read_cpu_temp() -> float:
    """Read SoC temperature from the Pi thermal zone. Returns 0.0 on non-Pi."""
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return int(f.read().strip()) / 1000.0
    except OSError:
        return 0.0


def update_system_gauges(snap: dict, cam_frame_counter: int) -> None:
    """
    Refresh all Gauge metrics from a telemetry snapshot + camera frame counter.
    Called every 5 s from the background sample loop in telemetry.py.
    """
    if snap.get("cpu_percent") is not None:
        CPU_PERCENT.set(snap["cpu_percent"])
    if snap.get("ram_used_mb") is not None:
        RAM_USED_BYTES.set(snap["ram_used_mb"] * 1024 * 1024)
        RAM_TOTAL_BYTES.set(snap["ram_total_mb"] * 1024 * 1024)
    if snap.get("uptime_seconds") is not None:
        UPTIME_SECONDS.set(snap["uptime_seconds"])
    if snap.get("estimated_distance_m") is not None:
        DISTANCE_METERS.set(snap["estimated_distance_m"])
    if snap.get("last_obstacle_cm") is not None:
        SONAR_DISTANCE_CM.set(snap["last_obstacle_cm"])

    CPU_TEMP_CELSIUS.set(read_cpu_temp())

    try:
        import psutil
        disk = psutil.disk_usage("/")
        DISK_USED_BYTES.set(disk.used)
        DISK_TOTAL_BYTES.set(disk.total)
    except Exception:
        pass

    # Camera frame counter delta → Prometheus Counter (only ever increases)
    global _cam_frame_last
    if cam_frame_counter > _cam_frame_last:
        CAMERA_FRAMES_TOTAL.inc(cam_frame_counter - _cam_frame_last)
        _cam_frame_last = cam_frame_counter


def update_active_sessions() -> None:
    """Query DB for live session count and update the gauge."""
    try:
        from app import db
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE expires_at > ?", (now,)
            ).fetchone()
        ACTIVE_SESSIONS.set(row[0] if row else 0)
    except Exception:
        pass
