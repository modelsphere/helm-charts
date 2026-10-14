{{- define "bodylog-victorialogs.name" -}}{{ default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}{{- end -}}
{{- define "bodylog-victorialogs.fullname" -}}
{{- if .Values.fullnameOverride -}}{{ .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else -}}{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}{{ .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else -}}{{ printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}{{- end -}}{{- end -}}
{{- end -}}
{{- define "bodylog-victorialogs.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{ include "bodylog-victorialogs.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}
{{- define "bodylog-victorialogs.selectorLabels" -}}
app.kubernetes.io/name: {{ include "bodylog-victorialogs.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
{{- define "bodylog-victorialogs.vectorSelectorLabels" -}}
{{ include "bodylog-victorialogs.selectorLabels" . }}
app.kubernetes.io/component: vector
{{- end -}}

{{/* The Vector Deployment's own labels. app.kubernetes.io/version is Vector's
     version here: the chart's appVersion tracks VictoriaLogs, which is the wrong
     number to stamp on the Vector workload. */}}
{{- define "bodylog-victorialogs.vectorLabels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{ include "bodylog-victorialogs.vectorSelectorLabels" . }}
app.kubernetes.io/version: {{ .Values.vector.image.tag | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/* The VictoriaLogs base URL: the bundled server's Service, or victorialogsUrl.
     Mirrors the subchart's naming: <release>-victorialogs-server, or
     <release>-server when the release name already contains "victorialogs". */}}
{{- define "bodylog-victorialogs.vlUrl" -}}
{{- if .Values.victorialogs.enabled -}}
{{- $server := .Values.victorialogs.server | default dict -}}
{{- $name := "" -}}
{{- if $server.fullnameOverride -}}{{- $name = $server.fullnameOverride -}}
{{- else if contains "victorialogs" .Release.Name -}}{{- $name = printf "%s-server" .Release.Name -}}
{{- else -}}{{- $name = printf "%s-victorialogs-server" .Release.Name -}}{{- end -}}
{{- printf "http://%s.%s.svc:9428" ($name | trunc 63 | trimSuffix "-") .Release.Namespace -}}
{{- else -}}
{{- required "victorialogs.enabled=false needs victorialogsUrl" .Values.victorialogsUrl | trimSuffix "/" -}}
{{- end -}}
{{- end -}}

{{- define "bodylog-victorialogs.checkpointPath" -}}
{{- .Values.vector.checkpointHostPath | default (printf "/var/lib/%s" (include "bodylog-victorialogs.fullname" .)) -}}
{{- end -}}
