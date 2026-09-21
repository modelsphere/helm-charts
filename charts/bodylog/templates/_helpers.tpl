{{- define "bodylog.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "bodylog.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}{{ .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else -}}{{ printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}{{- end -}}
{{- end -}}
{{- end -}}

{{- define "bodylog.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{ include "bodylog.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "bodylog.selectorLabels" -}}
app.kubernetes.io/name: {{ include "bodylog.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/* Secret name: an existing one if given, otherwise the one this chart creates */}}
{{- define "bodylog.secretName" -}}
{{- if .Values.secret.existingSecret -}}{{ .Values.secret.existingSecret }}
{{- else -}}{{ include "bodylog.fullname" . }}{{- end -}}
{{- end -}}

{{/* PVC name: an existing claim if given, otherwise the one this chart creates */}}
{{- define "bodylog.pvcName" -}}
{{- if .Values.persistence.existingClaim -}}{{ .Values.persistence.existingClaim }}
{{- else -}}{{ include "bodylog.fullname" . }}{{- end -}}
{{- end -}}
