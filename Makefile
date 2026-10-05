# ── RoboControl Makefile ───────────────────────────────────────────────────────
# Override defaults for a single command:
#   make deploy PI_HOST=ec2-user@10.0.0.5
#
# Or set permanently in .env (never committed):
#   echo "PI_HOST=ec2-user@10.0.0.5" >> .env

PI_HOST    ?= ec2-user@192.168.1.93
PI_DIR     ?= /home/ec2-user/robocontrol
SSH_KEY    ?= ~/.ssh/id_rsa
ADMIN_PASS ?=
PI_URL      = http://$(shell echo $(PI_HOST) | cut -d@ -f2):8000

SSH         = ssh -i $(SSH_KEY) $(PI_HOST)

.PHONY: help connect deploy restart logs logs-tail status check test open set-password

help:          ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

connect:       ## Open an SSH shell on the robot
	$(SSH) -t "cd $(PI_DIR) && exec bash -l"

_wait_ready = for i in 1 2 3 4 5 6 7 8 9 10; do \
	curl -s http://localhost:8000/api/ping && echo && break || sleep 2; done

deploy:        ## Push latest git commits to the robot and restart the service
	@echo "=== Deploying to $(PI_HOST) ==="
	git push origin master
	$(SSH) "cd $(PI_DIR) && git fetch origin && git reset --hard origin/master && venv/bin/pip install -r requirements.txt -q"
	$(SSH) "cd $(PI_DIR) && mkdir -p data/models && \
	  if [ ! -f data/models/mobilenet_ssd_v1.tflite ]; then \
	    echo 'Downloading ML model...' && \
	    wget -q https://storage.googleapis.com/download.tensorflow.org/models/tflite/coco_ssd_mobilenet_v1_1.0_quant_2018_06_29.zip \
	      -O /tmp/ssd_model.zip && \
	    unzip -p /tmp/ssd_model.zip detect.tflite > data/models/mobilenet_ssd_v1.tflite && \
	    rm /tmp/ssd_model.zip && \
	    echo 'Model downloaded.'; \
	  fi"
	$(SSH) "bash $(PI_DIR)/scripts/start.sh"
	@echo "Waiting for server..."
	$(SSH) "$(_wait_ready)"
	@if [ -n "$(ADMIN_PASS)" ]; then \
	  echo "--- Setting admin password ---"; \
	  $(SSH) "cd $(PI_DIR) && python3 -c \
	    \"import sys; sys.path.insert(0,'.'); import app.db as db; \
	    db.init_db(); db.update_admin_password(db.hash_password(sys.argv[1])); \
	    print('Admin password updated.')\" '$(ADMIN_PASS)'"; \
	fi
	@echo "=== Done — $(PI_URL) ==="

set-password:  ## Change the admin password on the robot (ADMIN_PASS=<new-password>)
	@test -n "$(ADMIN_PASS)" || (echo "Usage: make set-password ADMIN_PASS=<password>"; exit 1)
	$(SSH) "cd $(PI_DIR) && python3 -c \
	  \"import sys; sys.path.insert(0,'.'); import app.db as db; \
	  db.init_db(); db.update_admin_password(db.hash_password(sys.argv[1])); \
	  print('Admin password updated.')\" '$(ADMIN_PASS)'"

restart:       ## Restart the app on the robot without re-deploying
	$(SSH) "bash $(PI_DIR)/scripts/start.sh"
	@echo "Waiting for server..."
	$(SSH) "$(_wait_ready)"

logs:          ## Stream live logs from the robot
	$(SSH) "journalctl -u robocontrol -f"

logs-tail:     ## Show last 50 log lines from the robot
	$(SSH) "journalctl -u robocontrol -n 50 --no-pager"

status:        ## Check if the app is running on the robot
	$(SSH) "systemctl is-active robocontrol && \
	        curl -s http://localhost:8000/api/ping && echo"

check:         ## Quick remote health check (ping + git rev)
	@echo "=== Health check: $(PI_HOST) ==="
	$(SSH) "curl -s http://localhost:8000/api/ping && echo && \
	        cd $(PI_DIR) && git rev-parse --short HEAD && git log -1 --format='%s'"
	@echo "=== $(PI_URL) ==="

test:          ## Run the test suite locally (no hardware needed)
	python -m pytest tests/ -v

open:          ## Open the robot web UI in your browser
	open $(PI_URL)
