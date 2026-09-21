{{- define "cart.name" -}}{{ default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}{{- end -}}
{{- define "cart.fullname" -}}
{{- if .Values.fullnameOverride -}}{{ .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else -}}{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}{{ .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else -}}{{ printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}{{- end -}}{{- end -}}
{{- end -}}
{{- define "cart.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{ include "cart.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}
{{- define "cart.selectorLabels" -}}
app.kubernetes.io/name: {{ include "cart.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
{{/* Config ConfigMap name. Defaults to <fullname>-config, which is unique per
     release so several CARTs can share a namespace; override with configMapName.
     ModelRoute.cart.outputConfigMap may point either here or at an overlay. */}}
{{- define "cart.configMapName" -}}
{{- .Values.configMapName | default (printf "%s-config" (include "cart.fullname" .)) -}}
{{- end -}}

{{/* ---- configOverlays: extra keys in the same ConfigMap, appended as further
     -c files in list order. Later files win. ---- */}}

{{/* Validation: keys must be legal and unique, and at most one may be marked
     autoconfig -- which must not also carry content. */}}
{{- define "cart.configOverlays.check" -}}
{{- $seen := dict -}}
{{- $auto := 0 -}}
{{- range $i, $o := .Values.configOverlays -}}
{{- $k := $o.key | default "" -}}
{{- if not $k -}}{{- fail (printf "configOverlays[%d]: key is required" $i) -}}{{- end -}}
{{- if not (regexMatch "^[-._a-zA-Z0-9]+$" $k) -}}
{{- fail (printf "configOverlays[%d].key=%q is not a valid ConfigMap key: use letters, digits, '-', '_' or '.'" $i $k) -}}
{{- end -}}
{{- if eq $k "config.yaml" -}}
{{- fail (printf "configOverlays[%d].key cannot be config.yaml -- that key holds baseConfig" $i) -}}
{{- end -}}
{{- if hasKey $seen $k -}}{{- fail (printf "configOverlays: duplicate key %q" $k) -}}{{- end -}}
{{- $_ := set $seen $k true -}}
{{- if $o.autoconfig -}}
{{- $auto = add1 $auto -}}
{{- if hasKey $o "content" -}}
{{- fail (printf "configOverlays[%d] (%s): a key marked autoconfig is written by the controller, so the chart must not render it -- emit it once and helm upgrade will overwrite the workers written there" $i $k) -}}
{{- end -}}
{{- else if not (hasKey $o "content") -}}
{{- fail (printf "configOverlays[%d] (%s): set either content or autoconfig: true" $i $k) -}}
{{- end -}}
{{- end -}}
{{- if gt $auto 1 -}}
{{- fail (printf "configOverlays: at most one entry may set autoconfig: true, found %d" $auto) -}}
{{- end -}}
{{- end -}}

{{/* The key marked autoconfig: true, or empty. ModelRoute.cart.outputKey uses it. */}}
{{- define "cart.autoconfigKey" -}}
{{- range .Values.configOverlays -}}{{- if .autoconfig -}}{{- .key -}}{{- end -}}{{- end -}}
{{- end -}}

{{/* All config file paths, space separated, in load order (base first). */}}
{{- define "cart.configFiles" -}}
{{- $files := list "/workspace/configs/config.yaml" -}}
{{- range .Values.configOverlays -}}
{{- $files = append $files (printf "/workspace/configs/%s" .key) -}}
{{- end -}}
{{- join " " $files -}}
{{- end -}}
