{{- define "minimax-h3.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "minimax-h3.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "minimax-h3.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
app.kubernetes.io/name: {{ include "minimax-h3.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/*
  派生名统一走这里:先给后缀留位再截断基础名,保证结果 <= 63 且后缀不会被切掉。
  直接 `printf "%s-suffix" base | trunc 63` 是错的 ——
    * 后缀被切掉时,不同后缀会得到同一个名字(两个能力池撞名);
    * 或者拼完超过 63,k8s 直接拒(实测长 release 名下 <name>-server-api 到了 70 字符)。
  用法: include "minimax-h3.suffixed" (dict "root" . "suffix" "-server-api")
*/}}
{{/*
  数据库连接串。优先级:显式 database.url > 从 PG 的 Secret 自动拼。

  自动拼:PG chart 每次首装随机生成密码,人再手工抄一遍容易抄错、也会把密码带进
  values 和命令历史。这里直接 lookup PG 那个 Secret(跨 ns 读,需要装的人有该权限),
  取 <consumer>Password 拼出 DSN。lookup 在 `helm template`(不连集群)时返回空,
  那时会走下面的 fail —— 纯渲染请显式给 database.url。
*/}}
{{- define "minimax-h3.databaseUrl" -}}
{{- if .Values.database.url -}}
{{- .Values.database.url -}}
{{- else -}}
{{- $f := .Values.database.fromSecret -}}
{{- if and $f.name $f.namespace -}}
{{- $sec := lookup "v1" "Secret" $f.namespace $f.name -}}
{{- if and $sec $sec.data (hasKey $sec.data $f.passwordKey) -}}
{{- printf "postgresql://%s:%s@%s.%s.svc.cluster.local:%v/%s" $f.user (index $sec.data $f.passwordKey | b64dec) $f.name $f.namespace $f.port $f.database -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "minimax-h3.suffixed" -}}
{{- $suffix := .suffix -}}
{{- $budget := int (sub 63 (len $suffix)) -}}
{{- printf "%s%s" (include "minimax-h3.fullname" .root | trunc $budget | trimSuffix "-") $suffix -}}
{{- end -}}

{{- define "minimax-h3.server.fullname" -}}
{{/* 留 4 给 "-api"(<name>-server-api 是最长的直接派生名) */}}
{{- include "minimax-h3.suffixed" (dict "root" . "suffix" "-server") | trunc 59 | trimSuffix "-" -}}
{{- end -}}
{{/* 某个能力池的工作负载名:<release>-server-<pool> */}}
{{- define "minimax-h3.pool.fullname" -}}
{{/*
  截断必须**先给后缀留位**,不能拼完再一刀切到 63:
    * 池名后缀(-fl2va / -ref2va)被切掉的话,两个池会得到同一个名字 —— 资源重名;
    * 后面还要接 "-svc" 做 Service 名,拼完再截会超过 k8s 的 63 字符上限。
  所以预算 = 63 - len(池后缀) - 4(留给 "-svc"),先按预算截基础名,再拼后缀。
*/}}
{{- $suffix := printf "-%s" .pool -}}
{{- $budget := int (sub 63 (add (len $suffix) 4)) -}}
{{- $base := include "minimax-h3.server.fullname" .root | trunc $budget | trimSuffix "-" -}}
{{- printf "%s%s" $base $suffix -}}
{{- end -}}
{{/* 启用的池名(有序) */}}
{{- define "minimax-h3.enabledPools" -}}
{{- $names := list -}}
{{- range $name, $pool := .Values.pools -}}
{{- if $pool.enabled -}}{{- $names = append $names $name -}}{{- end -}}
{{- end -}}
{{- join " " (sortAlpha $names) -}}
{{- end -}}
{{- define "minimax-h3.sglang.fullname" -}}{{ include "minimax-h3.suffixed" (dict "root" . "suffix" "-sglang") }}{{- end -}}
{{- define "minimax-h3.router.fullname" -}}{{ include "minimax-h3.suffixed" (dict "root" . "suffix" "-router") }}{{- end -}}
{{- define "minimax-h3.dbSecretName" -}}
{{- if .Values.database.create -}}{{ include "minimax-h3.suffixed" (dict "root" . "suffix" "-db") }}{{- else -}}{{ required "database.existingSecret is required when database.create is false" .Values.database.existingSecret }}{{- end -}}
{{- end -}}
{{- define "minimax-h3.dbSecretKey" -}}
{{- if .Values.database.create -}}url{{- else -}}{{ .Values.database.existingSecretKey }}{{- end -}}
{{- end -}}

{{/* 共享资产盘的 PVC 名 */}}
{{- define "minimax-h3.sharedClaim" -}}
{{- if .Values.sharedStorage.create -}}{{ include "minimax-h3.suffixed" (dict "root" . "suffix" "-assets") }}{{- else -}}{{ required "sharedStorage.existingClaim is required when sharedStorage.create is false" .Values.sharedStorage.existingClaim }}{{- end -}}
{{- end -}}

{{/* Router 的后端地址:池的 ClusterIP Service。
     用 Service 而不是逐个 Pod 的 DNS —— k8s 只把流量发给 Ready 的 Pod,
     扩缩容也不用改 Router 的 env(改 replicaCount 不必重建 Router)。 */}}
{{- define "minimax-h3.poolUrls" -}}
{{- $root := .root -}}
{{- $pool := index $root.Values.pools .pool -}}
{{- if and $pool $pool.enabled -}}
{{- printf "http://%s-svc.%s.svc.cluster.local:%d"
      (include "minimax-h3.pool.fullname" (dict "root" $root "pool" .pool))
      $root.Release.Namespace ($root.Values.server.service.port | int) -}}
{{- end -}}
{{- end -}}

{{/* 校验 */}}
{{- define "minimax-h3.validate" -}}
{{/*
  router.enabled=false 且启用了多个能力池 —— 直接拒绝。
  此时对外只剩跨池共享的 <name>-server-api Service,它按 component=server 选中**所有**池的 Pod,
  k8s 会随机分发;而建任务接口只接受与本 Pod capability 相符的请求(server.py 里
  required_capability != capability 时返回 501),于是创建请求会间歇性 501。
  查询/删除/下载/Dashboard 走哪个 Pod 都一样(状态在库、文件在共享盘),只有"建任务"必须按能力分流。
*/}}
{{/* enabledPools 输出的是**空格**分隔;按逗号 split 会恒得到 1 个元素,拦截形同虚设 */}}
{{- $enabled := splitList " " (include "minimax-h3.enabledPools" .) -}}
{{- if and (not .Values.router.enabled) (gt (len $enabled) 1) -}}
{{- fail (printf "router.enabled=false 时只能启用一个能力池(当前启用 %d 个:%s)。跨池共享的 -api Service 会把建任务请求随机发给能力不符的 Pod,间歇性返回 501。要么开启 router 按能力分流,要么只留一个池。" (len $enabled) (include "minimax-h3.enabledPools" .)) -}}
{{- end -}}

{{- if .Values.server.enabled -}}
{{- if not (include "minimax-h3.enabledPools" .) -}}
{{- fail "pools 里至少要启用一个能力池(fl2va / ref2va)" -}}
{{- end -}}
{{- range $name, $pool := .Values.pools -}}
{{- if $pool.enabled -}}
{{- if not (has $name (list "fl2va" "ref2va")) -}}
{{- fail (printf "未知的能力池 %s:H3_SERVICE_CAPABILITY 只接受 fl2va / ref2va" $name) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{/*
  逐池校验:工作负载用的是顶层 sglang 与 pools.<name>.sglang **合并后**的配置,
  只查 .Values.sglang 的话,池级覆盖(比如 pools.fl2va.sglang.preStop.lingerSeconds=1)
  能绕过全部校验。
*/}}
{{- $lease := int .Values.server.config.leaseSeconds -}}
{{- range $name, $pool := .Values.pools -}}
{{- if $pool.enabled -}}
{{- $sg := fromYaml (include "minimax-h3.poolSglang" (dict "root" $ "pool" $name)) -}}
{{- if eq $name "ref2va" -}}
{{- include "minimax-h3.ref2vaTurbo.validate" (dict "root" $ "pool" $pool "sglang" $sg) -}}
{{- end -}}
{{- if and $sg.enabled $sg.preStop.enabled -}}
{{- $linger := int $sg.preStop.lingerSeconds -}}
{{- $deadline := int $sg.preStop.drainDeadlineSeconds -}}
{{- if le $linger $lease -}}
{{- fail (printf "池 %s:sglang.preStop.lingerSeconds (%d) 必须大于 server.config.leaseSeconds (%d) —— Server 排空超时放手后,在途任务要等租约到期由别的副本接管、并趁本 Pod IP 还在时取结果;linger 不够长就取不到,任务变僵尸" $name $linger $lease) -}}
{{- end -}}
{{/* grace 要盖住 preStop 的最坏路径:等排空到上限都没空,再 linger 一段,才发 SIGTERM */}}
{{- $grace := int $sg.terminationGracePeriodSeconds -}}
{{- $needed := add $deadline $linger -}}
{{- if le $grace $needed -}}
{{- fail (printf "池 %s:terminationGracePeriodSeconds (%d) 必须大于 preStop 最坏耗时 = drainDeadlineSeconds (%d) + lingerSeconds (%d) = %d;否则排空到一半就被 SIGKILL" $name $grace $deadline $linger $needed) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{/* 拼不出连接串就别往下走 —— 否则会渲染出 url: "" 的 Secret,H3 起来才报错 */}}
{{- if and .Values.database.create (not .Values.database.existingSecret) (not (include "minimax-h3.databaseUrl" .)) -}}
{{- if not .Values.database.fromSecret.name -}}
{{- fail "database: set database.url (postgresql://user:pass@host:5432/db), database.existingSecret, or database.fromSecret.namespace/name to read the password from the PostgreSQL chart's Secret" -}}
{{- end -}}
{{- fail (printf "拼不出数据库连接串:没有 database.url,也没能从 Secret %s/%s 读到 %s。helm template 不连集群(lookup 恒空),纯渲染请显式给 database.url;实际部署请确认 PG 已装、且当前身份能读它的 Secret" .Values.database.fromSecret.namespace .Values.database.fromSecret.name .Values.database.fromSecret.passwordKey) -}}
{{- end -}}
{{- if and (not .Values.sglang.enabled) (not .Values.server.externalSglangUrl) -}}
{{- fail "sglang.enabled=false 时必须设置 server.externalSglangUrl" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/* 池内 sglang 配置 = 公共默认 + 池覆盖 */}}
{{- define "minimax-h3.poolSglang" -}}
{{- $merged := mergeOverwrite (deepCopy .root.Values.sglang) (default (dict) (index .root.Values.pools .pool).sglang) -}}
{{- toYaml $merged -}}
{{- end -}}

{{/* Reject legacy overrides rather than silently detaching LoRA from sampling. */}}
{{- define "minimax-h3.ref2vaTurbo.validate" -}}
{{- $turbo := default (dict) .pool.turbo -}}
{{- if not (kindIs "bool" $turbo.enabled) -}}
{{- fail "pools.ref2va.turbo.enabled must be true or false" -}}
{{- end -}}
{{- range $key := list "ref2vaNumInferenceSteps" "ref2vaFlowShift" "ref2vaAudioFlowShift" -}}
{{- if hasKey $.root.Values.server.config $key -}}
{{- fail (printf "Legacy server.config.%s: migrate to pools.ref2va.turbo.sampling; use turbo.enabled=false for base Ref2VA" $key) -}}
{{- end -}}
{{- end -}}
{{- range .root.Values.server.extraEnv -}}
{{- if has .name (list "H3_REF2VA_NUM_INFERENCE_STEPS" "H3_REF2VA_FLOW_SHIFT" "H3_REF2VA_AUDIO_FLOW_SHIFT") -}}
{{- fail (printf "%s in server.extraEnv is managed by pools.ref2va.turbo.sampling; remove the duplicate" .name) -}}
{{- end -}}
{{- end -}}
{{- range .sglang.extraArgs -}}
{{- $flag := first (splitList "=" (toString .)) -}}
{{- if or (hasPrefix "--lora-" $flag) (has $flag (list "--warmup-mode" "--warmup-steps")) -}}
{{- fail (printf "%s in Ref2VA extraArgs is managed by the chart; migrate LoRA settings to pools.ref2va.turbo and remove warmup overrides" $flag) -}}
{{- end -}}
{{- end -}}
{{- range .sglang.extraVolumeMounts -}}
{{- if or (eq .name "ref2va-lora") (eq (trimSuffix "/" .mountPath) "/lora") -}}
{{- fail "Ref2VA /lora mount is managed by pools.ref2va.turbo; remove it from extraVolumeMounts" -}}
{{- end -}}
{{- end -}}
{{- range .sglang.extraVolumes -}}
{{- if eq .name "ref2va-lora" -}}
{{- fail "ref2va-lora volume is managed by pools.ref2va.turbo.lora.hostPath; remove it from extraVolumes" -}}
{{- end -}}
{{- end -}}
{{- if $turbo.enabled -}}
{{- if not .sglang.enabled -}}
{{- fail "pools.ref2va.turbo.enabled=true requires an in-Pod SGLang (sglang.enabled=true); external Turbo backends are not supported" -}}
{{- end -}}
{{- if not (hasPrefix "/" (required "pools.ref2va.turbo.lora.hostPath is required" $turbo.lora.hostPath)) -}}
{{- fail "pools.ref2va.turbo.lora.hostPath must be absolute" -}}
{{- end -}}
{{- $_ := required "pools.ref2va.turbo.lora.weightName is required" $turbo.lora.weightName -}}
{{- range $key := list "numInferenceSteps" "flowShift" "audioFlowShift" -}}
{{- $_ := required (printf "pools.ref2va.turbo.sampling.%s is required" $key) (index $turbo.sampling $key) -}}
{{- end -}}
{{- end -}}
{{- end -}}
