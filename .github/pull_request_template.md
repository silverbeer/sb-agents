Fixes SB-

## What

## Pillars ([docs/vision.md](../docs/vision.md))

Tick the ones this PR touches; explain any "no".

- [ ] **Messaging-agnostic:** no channel types outside an adapter; core and agents unchanged by channel work
- [ ] **Model-agnostic:** model from config; no provider SDK in agent logic; default changes backed by an eval
- [ ] **Framework-independent:** contract is A2A card + text + `{confidence, sources}`; no framework imports across packages
- [ ] **Local-first:** runs on the mini/k3s from this repo; secrets from 1Password only
- [ ] **Extensible:** a new specialist is a package + URL, no sb-support change
- [ ] **Observable:** `setup_telemetry` + `track_run`; trace joins the caller's; dashboards/alerts updated if behavior changed

## Tests
