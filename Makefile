.DEFAULT_GOAL := help
.PHONY: setup help check-update update build image-info pull-bot-image deploy stop run install uninstall install-pre-commit doctor tailscale-status emqx-auth emqx-audit emqx-auth-reset reset

# Command-line Make values are exported, never interpolated into shell code.
ifneq ($(origin API_IMAGE),undefined)
export API_IMAGE
endif
ifneq ($(origin BOT_IMAGE),undefined)
export BOT_IMAGE
endif
ifneq ($(origin EMQX_CONTAINER),undefined)
export EMQX_CONTAINER
endif
ifneq ($(origin ALLOW_PARTIAL_RESET),undefined)
export ALLOW_PARTIAL_RESET
endif

help:
	@printf '%s\n' \
	  'Usage: make <command> (default: help)' \
	  'Configuration' \
	  '  setup             Initialize configuration; preserve existing files' \
	  'Source' \
	  '  check-update      Fetch Git metadata and compare this checkout with origin/main' \
	  '  update            Fast-forward a clean main; no build or deployment' \
	  'Images' \
	  '  build             Build the local API image with source metadata; no restart' \
	  '  image-info        Inspect the local API image, not the running container' \
	  '  pull-bot-image    Pull the bot image explicitly (BOT_IMAGE overrides latest)' \
	  'Services' \
	  '  deploy            Build current source, then start/recreate services' \
	  '  stop              Stop this Compose stack, including its sidecar; keep data' \
	  'Development' \
	  '  install           Create the Conda environment and install commit hooks' \
	  '  install-pre-commit Install commit hooks in the existing Conda environment' \
	  '  uninstall         Remove the Conda environment; preserve runtime configuration' \
	  '  run               Validate endpoints, start dependencies, run development API' \
	  'Maintenance' \
	  '  doctor            Diagnose configuration, services, ports and authentication' \
	  '  tailscale-status  Inspect tailnet connection and forwarding' \
	  '  emqx-auth         Generate broker bootstrap credentials from .env' \
	  '  emqx-audit        Inspect broker authentication, ACLs and persisted rules' \
	  '  emqx-auth-reset   Destructive: recreate broker state with current credentials' \
	  '  reset             Destructive: remove this stack data and local credentials'

setup:
	@bash scripts/shell/setup.sh
check-update:
	@bash scripts/shell/source-update.sh check
update:
	@bash scripts/shell/source-update.sh update
build:
	@bash scripts/shell/build-image.sh
image-info:
	@bash scripts/shell/image-info.sh
pull-bot-image:
	@bash scripts/shell/stack.sh pull-bot-image
deploy:
	@bash scripts/shell/stack.sh deploy
stop:
	@bash scripts/shell/stack.sh stop
run:
	@bash scripts/shell/dev.sh run
install:
	@bash scripts/shell/dev.sh install
uninstall:
	@bash scripts/shell/dev.sh uninstall
install-pre-commit:
	@bash scripts/shell/dev.sh install-pre-commit
doctor:
	@bash scripts/shell/doctor.sh
tailscale-status:
	@bash scripts/shell/stack.sh tailscale-status
emqx-auth:
	@bash scripts/shell/emqx.sh auth
emqx-audit:
	@bash scripts/shell/emqx.sh audit
emqx-auth-reset:
	@bash scripts/shell/emqx.sh auth-reset
reset:
	@bash scripts/shell/reset.sh
