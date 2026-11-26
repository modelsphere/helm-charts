{{/*
  The chart renders the gateway-wide part only. Per-model routes (an Ingress
  llm-fleet-<name> each) and every plugin's per-route rule are written by
  autoconfig from ModelFleet objects. WasmPlugin matchRules is an atomic list:
  if this chart wrote any rule, each helm upgrade would drop autoconfig's.
*/}}
{{- define "higress-llm.validate" -}}
{{- $v := .Values -}}
{{- if lt (int64 $v.backendRouter.maxRequestBodyBytes) 16777216 }}{{ fail (printf "backendRouter.maxRequestBodyBytes %d is below ai-backend-router's own limit (16777216, set by autoconfig): a body the router accepts could not be replayed on spill." (int64 $v.backendRouter.maxRequestBodyBytes)) }}{{ end }}
{{- if not $v.streamGuard.detectors }}{{ fail "streamGuard.detectors must not be empty: autoconfig copies ai-stream-guard's defaultConfig into each fleet rule, and the plugin fails to load on a rule without detectors." }}{{ end }}
{{- end -}}
