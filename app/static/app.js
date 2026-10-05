// ── Tab switching ──────────────────────────────────────────────
function showTab(name, btn) {
  document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  if (btn) btn.classList.add('active');
}

// ── Notifications ──────────────────────────────────────────────
function notify(message, type) {
  type = type || 'info';
  const area = document.getElementById('notification-area');
  const el = document.createElement('div');
  el.className = 'notification ' + type;

  const text = document.createElement('span');
  text.textContent = message;

  const close = document.createElement('button');
  close.className = 'notif-close';
  close.textContent = '✕';
  close.onclick = function() { el.remove(); };

  el.appendChild(text);
  el.appendChild(close);
  area.appendChild(el);
  setTimeout(function() { if (el.parentElement) el.remove(); }, 5000);
}

// ── Stream reconnect ───────────────────────────────────────────
var _streamRetry = {};
var _streamDead = {};

function _setStreamDead(id, dead) {
  _streamDead[id] = dead;
  var overlay = document.getElementById(id + '-overlay');
  var corner  = document.getElementById(id + '-corner');
  if (overlay) overlay.style.display = dead ? 'flex' : 'none';
  if (corner)  corner.classList.toggle('spinning', dead);
}

// Replace the <img> with a fresh clone so the browser opens a brand-new
// connection — avoids stale MJPEG state without a full page reload.
function _replaceStreamImg(id) {
  var old = document.getElementById(id);
  if (!old || old.tagName !== 'IMG' || !old.parentNode) return;
  var base = old.getAttribute('data-src') || old.src.split('?')[0];
  var img = old.cloneNode(false);   // copies all attributes + inline handlers
  img.setAttribute('data-src', base);
  img.removeAttribute('src');
  old.parentNode.replaceChild(img, old);
  img.src = base + '?t=' + Date.now();
}

function onStreamError(id) {
  _setStreamDead(id, true);
  if (_streamRetry[id]) return;
  _streamRetry[id] = setTimeout(function() {
    _streamRetry[id] = null;
    _replaceStreamImg(id);
  }, 3000);
}

function onStreamLoad(id) {
  _setStreamDead(id, false);
  clearTimeout(_streamRetry[id]);
  _streamRetry[id] = null;
}

function reconnectStreams() {
  ['cam-stream', 'pip-cam'].forEach(function(id) {
    clearTimeout(_streamRetry[id]);
    _streamRetry[id] = null;
    var corner = document.getElementById(id + '-corner');
    if (corner) corner.classList.add('spinning');
    _replaceStreamImg(id);
  });
}

function reconnectAll() {
  _statusDisconnected = false;
  var btn = document.getElementById('reconnect-btn');
  if (btn) btn.style.display = 'none';
  reconnectStreams();
}

// ── Camera heartbeat — detect frozen/dead MJPEG streams ────────
// onerror is unreliable for <img> MJPEG; instead we poll /api/camera/health
// which returns the current frame counter. No change in N seconds = dead stream.
var _camLastCounter = -1;
var _camDeadTicks   = 0;
var CAM_DEAD_TICKS  = 3;   // 3 missed polls (~9 s) before showing overlay

function _pollCameraHealth() {
  fetch('/api/camera/health')
    .then(function(r) { return r.ok ? r.json() : null; })
    .then(function(d) {
      if (!d) return;
      if (d.counter === _camLastCounter) {
        _camDeadTicks++;
        if (_camDeadTicks >= CAM_DEAD_TICKS) {
          _setStreamDead('cam-stream', true);
          _setStreamDead('pip-cam',    true);
        }
      } else {
        _camLastCounter = d.counter;
        _camDeadTicks   = 0;
        _setStreamDead('cam-stream', false);
        _setStreamDead('pip-cam',    false);
      }
    })
    .catch(function() {});
}

setInterval(_pollCameraHealth, 3000);

// ── Status polling ─────────────────────────────────────────────
var _statusDisconnected = false;

function pollStatus() {
  fetch('/api/status')
    .then(function(r) { return r.json(); })
    .then(function(data) {
      var wasDisconnected = _statusDisconnected;
      _statusDisconnected = false;
      var dot = document.getElementById('status-dot');
      var msg = document.getElementById('status-msg');
      var servoDot = document.getElementById('servo-dot');
      dot.className = 'status-dot ' + (data.ok ? 'ok' : 'error');
      dot.title = data.message;
      msg.textContent = data.ok ? '' : data.message;
      if (servoDot) {
        servoDot.className = data.servo_ok ? 'status-dot ok' : 'status-dot';
        servoDot.title = data.servo_ok ? 'Servo: connected' : 'Servo: not connected';
      }
      if (wasDisconnected) {
        reconnectAll();
      }
    })
    .catch(function() {
      var dot = document.getElementById('status-dot');
      var servoDot = document.getElementById('servo-dot');
      var reconnectBtn = document.getElementById('reconnect-btn');
      dot.className = 'status-dot error';
      dot.title = 'Cannot reach server';
      if (servoDot) {
        servoDot.className = 'status-dot';
        servoDot.title = 'Servo: unknown';
      }
      if (reconnectBtn) reconnectBtn.style.display = 'inline-flex';
      if (!_statusDisconnected) {
        _statusDisconnected = true;
        notify('Connection lost — reconnecting automatically', 'alert');
      }
    });
}

setInterval(pollStatus, 3000);
pollStatus();

// ── Motor control ──────────────────────────────────────────────
var currentSpeed = typeof MOTOR_SPEED_DEFAULT !== 'undefined' ? MOTOR_SPEED_DEFAULT : 0.75;
var actionInterval = null;
var holdStart = null;
var HOLD_LOCK_MS = 3000;   // hold > 3 s → keep moving until stop is pressed
var actionLocked = false;

function updateSpeed(val) {
  currentSpeed = val / 100;
  document.getElementById('speed-value').textContent = val + '%';
}

function beepWarning() {
  try {
    var ctx = new (window.AudioContext || window.webkitAudioContext)();
    [0, 0.25].forEach(function(offset) {
      var osc = ctx.createOscillator();
      var gain = ctx.createGain();
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.type = 'square';
      osc.frequency.value = 880;
      gain.gain.setValueAtTime(0.25, ctx.currentTime + offset);
      gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + offset + 0.2);
      osc.start(ctx.currentTime + offset);
      osc.stop(ctx.currentTime + offset + 0.2);
    });
  } catch (e) {}
}

function sendMotor(action) {
  fetch('/api/motors/' + action, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ speed: currentSpeed })
  })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.blocked) {
        stopAction();
        beepWarning();
        notify(data.message || 'Obstacle detected!', 'alert');
      } else if (data.detail && !data.detail.ok) {
        notify(data.detail.message || 'Motor error', 'warning');
      }
    })
    .catch(function() { notify('Connection lost', 'alert'); });
}

function _setLockIndicator(locked) {
  var btn = document.getElementById('stop-btn');
  if (btn) btn.style.outline = locked ? '3px solid #f9a825' : '';
}

function startAction(action) {
  if (actionLocked) {
    // Switch to new direction without stopping
    clearInterval(actionInterval);
    actionInterval = null;
  }
  holdStart = Date.now();
  actionLocked = false;
  _setLockIndicator(false);
  clearInterval(actionInterval);
  actionInterval = null;
  sendMotor(action);
  actionInterval = setInterval(function() { sendMotor(action); }, 200);
}

function releaseAction() {
  if (!holdStart) return;
  var held = Date.now() - holdStart;
  holdStart = null;
  if (held >= HOLD_LOCK_MS) {
    // Held long enough → lock, keep moving, show indicator
    actionLocked = true;
    _setLockIndicator(true);
    notify('Locked — press Stop to halt', 'info');
    return;
  }
  stopAction();
}

function stopAction() {
  clearInterval(actionInterval);
  actionInterval = null;
  holdStart = null;
  actionLocked = false;
  _setLockIndicator(false);
  sendMotor('stop');
}

// ── Auto-drive ─────────────────────────────────────────────────
var autoDriveRunning = false;
var _autoDrivePoll = null;

function _autoDriveButtons() {
  return document.querySelectorAll('.autodrive-btn');
}

function _renderAutoDriveState(running, action, distanceCm) {
  autoDriveRunning = running;
  _autoDriveButtons().forEach(function(btn) {
    btn.textContent = running ? '🤖 Auto-Drive   ON' : '🤖 Auto-Drive   OFF';
    btn.classList.toggle('active', running);
  });
  var statusText = '';
  if (running) {
    if (action === 'avoiding') {
      statusText = 'avoiding obstacle (' + distanceCm + ' cm)';
    } else if (action === 'forward') {
      statusText = distanceCm != null ? 'driving forward (' + distanceCm + ' cm clear)' : 'driving forward';
    } else {
      statusText = 'starting…';
    }
  }
  document.querySelectorAll('.autodrive-status').forEach(function(el) {
    el.textContent = statusText;
  });
}

function _pollAutoDriveStatus() {
  fetch('/api/motors/auto/status')
    .then(function(r) { return r.json(); })
    .then(function(d) {
      _renderAutoDriveState(d.running, d.action, d.distance_cm);
      if (!d.running && _autoDrivePoll) {
        clearInterval(_autoDrivePoll);
        _autoDrivePoll = null;
      }
    })
    .catch(function() {});
}

function autoDriveClick() {
  var endpoint = autoDriveRunning ? '/api/motors/auto/stop' : '/api/motors/auto/start';
  fetch(endpoint, { method: 'POST' })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.detail && !data.detail.ok) {
        notify(data.detail.message || 'Auto-drive error', 'warning');
        return;
      }
      _renderAutoDriveState(data.running, null, null);
      if (data.running && !_autoDrivePoll) {
        _autoDrivePoll = setInterval(_pollAutoDriveStatus, 500);
      }
    })
    .catch(function() { notify('Connection lost', 'alert'); });
}

// ── Camera pan/tilt ────────────────────────────────────────────
function sendCamera(action) {
  fetch('/api/camera/' + action, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: '{}'
  })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.detail && !data.detail.ok) {
        notify(data.detail.message || 'Camera error', 'warning');
      }
    })
    .catch(function() { notify('Connection lost', 'alert'); });
}

// ── Telemetry ──────────────────────────────────────────────────
function fmtUptime(s) {
  var h = Math.floor(s / 3600);
  var m = Math.floor((s % 3600) / 60);
  var sec = s % 60;
  return (h ? h + 'h ' : '') + (h || m ? m + 'm ' : '') + sec + 's';
}

// ok is a boolean from the API — safe to use innerHTML (no user input interpolated)
function batteryLabel(ok) {
  return ok
    ? '<span class="tel-latency-dot" style="background:#2dc653"></span>Connected'
    : '<span class="tel-latency-dot" style="background:#e53935"></span>Not detected';
}

function _barColor(pct) {
  return pct < 60 ? '#2dc653' : pct < 85 ? '#f9a825' : '#e53935';
}

function pollTelemetry() {
  fetch('/api/telemetry')
    .then(function(r) { return r.json(); })
    .then(function(d) {
      // CPU
      var cpuEl  = document.getElementById('tel-cpu');
      var cpuBar = document.getElementById('tel-cpu-bar');
      if (cpuEl && d.cpu_percent != null) {
        cpuEl.textContent = d.cpu_percent + '%';
        if (cpuBar) { cpuBar.style.width = d.cpu_percent + '%'; cpuBar.style.background = _barColor(d.cpu_percent); }
      }

      // GPU (card only present when the board exposes one, e.g. Jetson)
      var gpuEl    = document.getElementById('tel-gpu');
      var gpuBar   = document.getElementById('tel-gpu-bar');
      var gpuClock = document.getElementById('tel-gpu-clock');
      if (gpuEl && d.gpu_percent != null) {
        gpuEl.textContent = d.gpu_percent + '%';
        if (gpuBar) { gpuBar.style.width = d.gpu_percent + '%'; gpuBar.style.background = _barColor(d.gpu_percent); }
        if (gpuClock && d.gpu_clock_mhz != null) gpuClock.textContent = d.gpu_clock_mhz + ' MHz';
      }

      // RAM
      var ramEl  = document.getElementById('tel-ram');
      var ramBar = document.getElementById('tel-ram-bar');
      if (ramEl && d.ram_percent != null) {
        ramEl.textContent = d.ram_used_mb + ' / ' + d.ram_total_mb + ' MB';
        if (ramBar) { ramBar.style.width = d.ram_percent + '%'; ramBar.style.background = _barColor(d.ram_percent); }
      }

      // Uptime
      document.getElementById('tel-uptime').textContent = fmtUptime(d.uptime_seconds);

      // Battery status (no ADC — show connection state)
      var motorBatEl = document.getElementById('tel-battery-motor');
      var piBatEl = document.getElementById('tel-battery-pi');
      if (motorBatEl) motorBatEl.innerHTML = batteryLabel(d.battery_motor_ok);
      if (piBatEl) piBatEl.innerHTML = batteryLabel(d.battery_pi_ok);

      // Board battery label — reflects the actual detected device (Pi/Jetson/etc.)
      var boardLabelEl = document.getElementById('tel-battery-board-label');
      var boardSubEl = document.getElementById('tel-battery-board-sub');
      if (boardLabelEl && d.board_battery_label) boardLabelEl.textContent = d.board_battery_label;
      if (boardSubEl && d.board_battery_sub) boardSubEl.textContent = d.board_battery_sub;

      // Latency
      var latEl = document.getElementById('tel-latency');
      var latBar = document.getElementById('tel-latency-bar');
      if (d.last_command_ms !== null && d.last_command_ms !== undefined) {
        latEl.textContent = d.last_command_ms + ' ms';
        var color = d.last_command_ms < 30 ? '#2dc653'
                  : d.last_command_ms < 80 ? '#f9a825'
                  : '#e53935';
        latBar.innerHTML = '<span class="tel-latency-dot" style="background:' + color + '"></span>'
          + (d.last_command_ms < 30 ? 'Fast' : d.last_command_ms < 80 ? 'OK' : 'Slow');
      } else {
        latEl.textContent = '—';
        latBar.textContent = '';
      }

      // Counters
      document.getElementById('tel-commands').textContent = d.commands_sent;
      document.getElementById('tel-obstacles').textContent = d.obstacles_detected;

      var obsEl = document.getElementById('tel-last-obstacle');
      obsEl.textContent = d.last_obstacle_cm !== null && d.last_obstacle_cm !== undefined
        ? 'last: ' + d.last_obstacle_cm + ' cm' : '';

      // Distance
      var distM = d.estimated_distance_m;
      document.getElementById('tel-distance').textContent =
        distM >= 1 ? distM.toFixed(1) + ' m' : Math.round(distM * 100) + ' cm';
    })
    .catch(function() {});
}

setInterval(pollTelemetry, 5000);
pollTelemetry();

// Pick up auto-drive state on page load (e.g. after a refresh while it's
// still running) and resume the fast status poll if so.
fetch('/api/motors/auto/status')
  .then(function(r) { return r.json(); })
  .then(function(d) {
    _renderAutoDriveState(d.running, d.action, d.distance_cm);
    if (d.running) _autoDrivePoll = setInterval(_pollAutoDriveStatus, 500);
  })
  .catch(function() {});

// ── Mission stubs ──────────────────────────────────────────────
function sendMission(action) {
  fetch('/api/missions/' + action, { method: 'POST' })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.detail) notify(data.detail.message, 'info');
    })
    .catch(function() { notify('Connection lost', 'alert'); });
}
