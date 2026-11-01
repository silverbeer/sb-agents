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

.PHONY: obs-up obs-down obs-logs docs-a2a support ask adk-ask test lint

obs-up:
	@docker start $(LGTM_NAME) 2>/dev/null || docker run -d --name $(LGTM_NAME) \
		-p 3000:3000 -p 4317:4317 -p 4318:4318 $(LGTM_IMAGE)
	@echo "Grafana: http://localhost:3000  OTLP: http://localhost:4318"

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
