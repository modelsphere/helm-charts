{{- define "llm-slo.namespace" -}}
{{- .Values.namespace -}}
{{- end -}}

{{- define "llm-slo.labels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: Helm
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end -}}

{{/*
Advisory check only. Helm's .Capabilities.APIVersions is empty under plain
`helm template` (no cluster), so a hard fail would falsely trigger for
offline lint/template runs. Instead of blocking, we emit a NOTES.txt hint
when the CRDs look absent. Runtime failure mode is still clear: CRs from
downstream charts fail to apply with "no matches for kind ..." and point
right back at llmscaleoperator.
*/}}
