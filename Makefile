# Local observability stack: grafana/otel-lgtm (OTel Collector, Tempo, Loki,
# Prometheus, Grafana in one container). Dev/demo only, per Grafana.
#   make obs-up    then open http://localhost:3000 (admin/admin)
#   make docs-a2a / support / ask Q="..."   run an agent exporting to it

LGTM_IMAGE ?= grafana/otel-lgtm:0.37.0
LGTM_NAME  ?= sb-otel-lgtm
OP         := op run --env-file=.env.op --
OBS_ENV    := OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 \
              SB_OBS_CAPTURE_CONTENT=$(or $(SB_OBS_CAPTURE_CONTENT),1) \
              PYDANTIC_AI_NO_BANNER=1

.PHONY: obs-up obs-down obs-logs dashboards docs-a2a support ask adk-ask test lint

GRAFANA_DIR := $(CURDIR)/observability/grafana
PROVISIONING := /otel-lgtm/grafana/conf/provisioning

# Alert thresholds (SB-1487). Override to fire an alert on purpose, e.g.
#   make obs-down obs-up ALERT_DAILY_USD=0.001
ALERT_FAILED_RUNS ?= 1
ALERT_P95_MS      ?= 20000
ALERT_DAILY_USD   ?= 1

RENDERED := $(GRAFANA_DIR)/.rendered

# Dashboards and alerting are provisioned from the repo. Thresholds and mounts are read at
# container start: `make obs-down obs-up` to change them (container data is not persisted).
# Grafana parses YAML before expanding ${VARS}, so thresholds (numbers inside flow lists) are
# substituted here into a gitignored copy; only ${SLACK_BOT_TOKEN} is left for Grafana.
# SLACK_BOT_TOKEN comes from 1Password via op run and is passed by name only (-e NAME).
obs-up:
	@mkdir -p $(RENDERED)
	@sed -e 's/$${ALERT_FAILED_RUNS}/$(ALERT_FAILED_RUNS)/g' \
		-e 's/$${ALERT_P95_MS}/$(ALERT_P95_MS)/g' \
		-e 's/$${ALERT_DAILY_USD}/$(ALERT_DAILY_USD)/g' \
		$(GRAFANA_DIR)/provisioning/sb-agents-alerting.yaml > $(RENDERED)/sb-agents-alerting.yaml
	@docker start $(LGTM_NAME) 2>/dev/null || $(OP) docker run -d --name $(LGTM_NAME) \
		-p 3000:3000 -p 4317:4317 -p 4318:4318 \
		-e SLACK_BOT_TOKEN \
		-v $(GRAFANA_DIR)/dashboards:/sb-agents/dashboards:ro \
		-v $(GRAFANA_DIR)/provisioning/sb-agents-dashboards.yaml:$(PROVISIONING)/dashboards/sb-agents.yaml:ro \
		-v $(RENDERED)/sb-agents-alerting.yaml:$(PROVISIONING)/alerting/sb-agents.yaml:ro \
		$(LGTM_IMAGE)
	@echo "Grafana: http://localhost:3000  OTLP: http://localhost:4318"

dashboards:
	uv run python observability/grafana/build_dashboards.py

obs-down:
	docker rm -f $(LGTM_NAME)

obs-logs:
	docker logs -f $(LGTM_NAME)

docs-a2a:
	$(OBS_ENV) $(OP) uv run docs-agent-a2a

support:
	$(OBS_ENV) $(OP) uv run sb-support

ask:
	$(OBS_ENV) $(OP) uv run docs-agent ask -v "$(Q)"

adk-ask:
	$(OBS_ENV) $(OP) uv run adk-docs-client "$(Q)"

test:
	PYDANTIC_AI_NO_BANNER=1 uv run pytest -q

lint:
	uv run ruff check . && uv run ruff format --check .
