# Runs RoboControl with SIMULATE=true — no GPIO, no rrb3, no camera
# hardware — so you can exercise the dashboard, motor commands, telemetry
# and camera stream on a laptop before deploying to a Raspberry Pi or
# Jetson Nano. See docker/README.md.
#
# Build from the REPO ROOT (this Dockerfile needs the full app/ tree):
#   docker build -f docker/Dockerfile -t robocar-sim .
#   docker run --rm -p 8000:8000 robocar-sim
#
# Or: docker compose -f docker/docker-compose.yml up --build

FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt ./
# GPIO/rrb3/picamera2/opencv are board-specific and never installed here —
# app/hardware/* already degrade gracefully without them, and SIMULATE=true
# (set below) bypasses them entirely at runtime, so a plain requirements.txt
# install is enough.
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

COPY app ./app

ENV SIMULATE=true \
    DATA_DIR=/data \
    PYTHONUNBUFFERED=1

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
