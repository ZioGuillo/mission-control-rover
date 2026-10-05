import threading
import time

_start = time.monotonic()

# Accumulated session stats (reset on server restart)
_state = {
    "commands_sent": 0,
    "obstacles_detected": 0,
    "last_obstacle_cm": None,
    "last_command_ms": None,
    # Weighted motor-on seconds: sum of (speed × 0.2s) per command pulse
    "_motor_speed_seconds": 0.0,
}

# Rough conversion: RRB3 motors at full speed ≈ 30 cm/s
_CM_PER_SPEED_SECOND = 30.0

# ── System stats (sampled in background every 5 s) ──────────────
_sys = {"cpu_percent": None, "ram_used_mb": None,
        "ram_total_mb": None, "ram_percent": None}

try:
    import psutil as _psutil
    _psutil_ok = True
except Exception:
    _psutil_ok = False


def _sample_loop() -> None:
    # First call primes the counter; subsequent calls return the delta.
    if _psutil_ok:
        _psutil.cpu_percent(interval=None)
    while True:
        time.sleep(5)
        if not _psutil_ok:
            continue
        try:
            cpu = round(_psutil.cpu_percent(interval=None), 1)
            mem = _psutil.virtual_memory()
            _sys["cpu_percent"]  = cpu
            _sys["ram_used_mb"]  = round(mem.used  / 1024 / 1024)
            _sys["ram_total_mb"] = round(mem.total / 1024 / 1024)
            _sys["ram_percent"]  = round(mem.percent, 1)
        except Exception:
            pass
        # Push fresh values into Prometheus gauges
        try:
            from app import metrics as _m
            from app.hardware import camera_driver as _cam
            _m.update_system_gauges(snapshot(), _cam._frame_counter)
            _m.update_active_sessions()
        except Exception:
            pass


threading.Thread(target=_sample_loop, daemon=True).start()


def _sys_stats() -> dict:
    return dict(_sys)


def record_command(latency_ms: float, action: str, speed: float) -> None:
    _state["commands_sent"] += 1
    _state["last_command_ms"] = round(latency_ms, 1)
    if action in ("forward", "reverse"):
        _state["_motor_speed_seconds"] += speed * 0.2
    elif action in ("left", "right"):
        _state["_motor_speed_seconds"] += (speed / 2) * 0.2


def record_obstacle(dist_cm: float) -> None:
    _state["obstacles_detected"] += 1
    _state["last_obstacle_cm"] = round(dist_cm, 1)


def snapshot() -> dict:
    uptime = time.monotonic() - _start
    dist_m = (_state["_motor_speed_seconds"] * _CM_PER_SPEED_SECOND) / 100.0
    data = {
        "uptime_seconds": int(uptime),
        "commands_sent": _state["commands_sent"],
        "obstacles_detected": _state["obstacles_detected"],
        "last_obstacle_cm": _state["last_obstacle_cm"],
        "last_command_ms": _state["last_command_ms"],
        "estimated_distance_m": round(dist_m, 2),
    }
    data.update(_sys_stats())
    return data
