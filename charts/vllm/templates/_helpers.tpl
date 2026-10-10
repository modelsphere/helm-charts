{{/*
  The name every resource this chart owns is called.

  Defaults to the release name, unadorned. One release serves one model, so the
  release name IS the name, and nothing needs a suffix to stay unique -- the two
  components that would otherwise collide, the metrics mock and the CART
  subchart, keep their own suffixed identity and are deliberately NOT routed
  through this helper.

  fullnameOverride exists for one reason: releases installed before the resource
  names were simplified, when the engine Deployment was <release>-vllm.
  Renaming a live Deployment is a delete-and-create, not a rolling update, and on
  a GPU node the replacement cannot even schedule until the old pod releases its
  GPU -- so the upgrade costs the full termination grace period plus a cold model
  load, rather than a rolling restart. Pinning

      fullnameOverride: <release>-vllm

  keeps that Deployment exactly where it is and makes the upgrade ordinary.

  It does not restore the older Service (<release>-vllm-svc) or LLMScaler
  (<release>-scaler): those carried different suffixes, and one value cannot be
  three names. Both are still recreated, which costs a new ClusterIP and an
  operator re-adopt but no pod restart -- and the route survives it, because the
  ModelRoute's top peer tier is direct pod IPs rather than the Service.

  Leave it empty on new releases.
*/}}
{{- define "vllm.fullname" -}}
{{- .Values.fullnameOverride | default .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
  The id this service is known by OUTSIDE the cluster's object graph: what the
  decision server, the SLO operator and the route all index it under. Not a
  Kubernetes name and never looked up from an object, which is why it gets its
  own value rather than reusing vllm.fullname directly -- but fullname is the
  sensible default, so an unset serviceId costs nothing.
*/}}
{{- define "vllm.serviceId" -}}
{{- .Values.serviceId | default (include "vllm.fullname" .) -}}
{{- end -}}

{{/*
  The engine's own Service. Under lws.enabled it is <fullname>-leader, because
  the LWS controller already owns a headless Service named <fullname> (the
  group's DNS domain) and two Services cannot share a name.
*/}}
{{- define "vllm.serviceName" -}}
{{- $name := include "vllm.fullname" . -}}
{{- if .Values.lws.enabled -}}
{{- $name = printf "%s-leader" $name -}}
{{- end -}}
{{- $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
  Everything the shutdown does has to fit inside terminationGracePeriodSeconds.
  That timer starts when the pod is marked Terminating and covers BOTH the
  preStop hook and vLLM finishing up after SIGTERM. If the total is too big the
  kubelet kills the pod while requests are still running, so fail here rather
  than ship a config that quietly drops them.

  lifecycle.shutdownReserveSeconds is headroom after SIGTERM for vLLM's own exit
  (and, under preStopKill, how long the hook waits after its own SIGTERM on top
  of shutdownTimeout before killing PID 1's children).

  Renders nothing -- it either fails the release or gets out of the way. Both
  the Deployment and the LeaderWorkerSet call it, because both carry the same
  preStop hook and the same grace period.
*/}}
{{- define "vllm.shutdownBudget" -}}
{{- $preStop := .Values.lifecycle.preStop -}}
{{- $budget := add (int .Values.lifecycle.shutdownTimeout) (int .Values.lifecycle.shutdownReserveSeconds) -}}
{{- if $preStop.enabled -}}
{{- $budget = add $budget (int $preStop.endpointSyncSeconds) (int $preStop.drainSeconds) -}}
{{- end -}}
{{- if gt (int $budget) (int .Values.terminationGracePeriodSeconds) -}}
{{- fail (printf "vllm: terminationGracePeriodSeconds (%d) is smaller than the shutdown budget (%d = preStop endpointSyncSeconds %d + drainSeconds %d + lifecycle.shutdownTimeout %d + lifecycle.shutdownReserveSeconds %d); the pod would be SIGKILLed mid-drain" (int .Values.terminationGracePeriodSeconds) (int $budget) (int $preStop.endpointSyncSeconds) (int $preStop.drainSeconds) (int .Values.lifecycle.shutdownTimeout) (int .Values.lifecycle.shutdownReserveSeconds)) -}}
{{- end -}}
{{- end -}}

{{/*
  The labels every engine pod carries on top of the chart's own (app, role):
  podLabels, plus rdma-ib: "true" when rdma.enabled under lws.enabled OR under
  pd's DisaggregatedSet shape -- the label rdma-injector keys off. A podLabels
  entry of the same name wins, so a hand-written one is neither duplicated
  (which would be invalid YAML) nor overridden.

  Colocate pd pods talk NIXL over cuda_ipc inside one netns and never touch
  IB, so they get no rdma-ib label here even when rdma.enabled is set.

  Renders nothing when there is nothing to add.
*/}}
{{- define "vllm.podLabels" -}}
{{- $labels := deepCopy (.Values.podLabels | default dict) -}}
{{- $needsRdma := or .Values.lws.enabled (and .Values.pd.enabled (not .Values.pd.colocate)) -}}
{{- if and .Values.rdma.enabled $needsRdma -}}
{{- $labels = merge $labels (dict "rdma-ib" "true") -}}
{{- end -}}
{{- with $labels }}{{ toYaml . }}{{ end -}}
{{- end -}}
