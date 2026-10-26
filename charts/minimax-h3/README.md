# minimax-h3

A self-hosted, MiniMax H3-compatible video generation service for the open-weight
MiniMax H3 models: the H3 Router, the H3 Server and SGLang in one release. Source and
API documentation: [modelsphere/minimax-h3-server](https://github.com/modelsphere/minimax-h3-server).

This is an independent project, not affiliated with or endorsed by MiniMax.

```
client ──► Router (Deployment, stateless, optional)
             │
             ├─► fl2va pool   Server Pod ×N ┐ each Pod: container server ──► container sglang (4 GPUs)
             │                              │   FL2VA weights, --tp-size 4 --ulysses-degree 1
             └─► ref2va pool  Server Pod ×M ┘   Ref2VA weights + Turbo LoRA, --warmup-mode request
                     │
                     └──── shared asset volume (RWX PVC) + PostgreSQL
```

- **Two capability pools, sized separately** (`pools.fl2va.replicaCount`,
  `pools.ref2va.replicaCount`). Pools differ only in `H3_SERVICE_CAPABILITY` (set from the
  pool name) and in the SGLang weights and parallelism; everything else comes from the shared
  `server:` / `sglang:` sections, which a pool's own `sglang:` overrides. Tasks carry their
  `required_capability`, so both pools share one database without picking up each other's work.
- **Server and SGLang share a Pod.** A Server has exactly one backend and one task worker,
  and SGLang runs one generation at a time, so one-to-one wastes nothing. **More concurrency
  means more replicas.**
- **`H3_SGLANG_URL` is the Pod IP, not 127.0.0.1.** The Server stores the backend URL with
  each task; when a lease expires another replica takes the task over and polls that URL. A
  localhost URL would send it to its own SGLang, where the job does not exist.
- **Shared asset volume.** Server and SGLang must see the same directory at the same path, so
  every Pod mounts one RWX PVC. A block volume (RWO) does not work: replicas run on different
  nodes. The PVC carries `helm.sh/resource-policy: keep`, so uninstalling the chart keeps the data.
- **The Router targets each pool's ClusterIP Service** (`<release>-server-<pool>-svc`), so only
  Ready Pods receive traffic and changing `replicaCount` needs no Router restart. Headless
  Services remain for the ServiceMonitor to scrape each Pod.
- **The Router is optional.** With `router.enabled: false`, the shared `<release>-server-api`
  Service serves directly (query, delete, download and dashboard work through any Server);
  only one pool may be enabled in that shape.
- **PostgreSQL is external.** Point `database.url` at it, or let the chart read the password
  from the PostgreSQL chart's Secret (`database.fromSecret`). Secrets cannot be referenced
  across namespaces, so the chart writes a DSN-only Secret in its own namespace.

## Prerequisites

| Dependency | Notes |
| --- | --- |
| PostgreSQL | Any reachable instance with a database for H3 (the examples use `minimaxh3`) |
| RWX StorageClass | CephFS, NFS or similar; `sharedStorage.storageClass` empty uses the cluster default |
| GPUs | 4 per SGLang replica (`nvidia.com/gpu: 4`), NVIDIA device plugin or GPU Operator |
| Weights | hostPath `/data/models/MiniMax-H3/{FL2VA,Ref2VA}` by default, **synced to the GPU nodes first**; a PVC works too |

## Install

```bash
helm repo add modelsphere https://modelsphere.github.io/helm-charts
helm install h3 modelsphere/minimax-h3 -n minimax-h3 --create-namespace -f my-values.yaml
```

A typical `my-values.yaml`:

```yaml
database:
  url: postgresql://h3:<password>@postgresql.db.svc.cluster.local:5432/minimaxh3
sharedStorage:
  storageClass: cephfs
pools:                        # both pools are on by default; disable one you do not need
  fl2va:
    replicaCount: 2
    sglang:
      model: {hostPath: /srv/models/MiniMax-H3/FL2VA}
  ref2va:
    replicaCount: 1
    sglang:
      model: {hostPath: /srv/models/MiniMax-H3/Ref2VA}
    turbo:
      lora: {hostPath: /srv/models/Minimax-h3-Turbo}
router:
  service:
    type: NodePort            # direct access for testing; keep ClusterIP behind a gateway
    nodePort: 30880
server:
  config:
    publicBaseUrl: https://h3.example.com   # see "Behind a gateway"
```

Without GPU quota, run the control plane only and point it at an existing SGLang:

```yaml
sglang:
  enabled: false
server:
  externalSglangUrl: http://sglang.example.internal:30010   # shared by every replica
pools:
  ref2va:
    turbo: {enabled: false}   # Turbo needs the in-Pod SGLang, see Ref2VA
```

## Behind a gateway

**Download URLs point back at H3** (`<base>/v2/video_generation/<task_id>/content`), not at
object storage. By default the base is built from the request's `Host`,
`X-Forwarded-Host` and `X-Forwarded-Proto`. Behind an API gateway, set
`server.config.publicBaseUrl`: the Host the Server sees is then an internal address the client
cannot reach, and when H3 sits under a gateway sub-path (`https://gw.example.com/h3`) the
derived base loses the prefix. The setting applies to existing tasks and callback payloads too.

The gateway must also:

- accept request bodies of at least 64 MiB (I2V accepts base64 images; the Router refuses
  larger bodies itself);
- pass the `Range` header through (downloads support HTTP ranges);
- allow long-lived downloads (no short read/send timeouts).

## Probes

| Probe | Endpoint | On failure |
| --- | --- | --- |
| liveness | `/healthz` | container restart |
| readiness | `/readyz` | removed from Service endpoints |
| startup | `/healthz` | holds the other two off until the process is up |

**`/readyz` reflects database health**: with the database unavailable it returns 503 and the
replica leaves the Service. Turn it off with `server.config.readinessChecksDatabase: false`.

| `readinessChecksDatabase` | `/readyz` during a DB outage | `/healthz` | task create | Pod restarts |
| --- | --- | --- | --- | --- |
| `true` (default) | **503** (out of the Service) | 200 | 500 | 0 |
| `false` | 200 | 200 | 500 | 0 |

With it on, a database blip makes **every replica NotReady at once**; with it off, requests keep
arriving and fail with 500. During a full outage the two behave the same. What it buys is that
replicas which alone cannot reach the database step aside, and that a wrong DSN stalls a
rolling update instead of replacing every replica with a broken one.

Two rules go the other way on purpose:

- **liveness does not check the database**, or an outage would restart every replica in a loop;
- **startup uses `/healthz`, not `/readyz`**, so a database still coming up during an install
  does not fail the Pod's startup window.

The SGLang container's probes check that the model is resident on the GPUs
(`files/sglang-model-loaded.py`), not just HTTP `/health`: after a CUDA OOM kills a diffusion
worker, the main process stays up and `/health` keeps answering 200.

## Termination and draining

Deleting a Pod or rolling an update keeps in-flight tasks by default:

```yaml
sglang:
  preStop: {enabled: true, drainDeadlineSeconds: 600, lingerSeconds: 360}
  terminationGracePeriodSeconds: 1200
server:
  config: {leaseSeconds: 300}
```

1. **SGLang's own graceful shutdown times out under diffusion.** On SIGTERM it asks the
   scheduler to drain, but a worker inside the denoising loop cannot answer within SGLang's
   fixed 5 s shutdown timeout, so it falls back to terminate and kill, losing the running job.
   The preStop hook therefore **waits until SGLang is idle and then sends SIGTERM itself**;
   an idle scheduler answers at once and shuts down cleanly.
2. **It lingers after draining.** The server container has no slow shutdown, so the bookkeeping
   for its in-flight task is finished by **another replica** that takes the lease over and polls
   the stored backend URL, which is this Pod's IP. The Pod must outlive that takeover, which
   happens at the latest when the lease expires: **`lingerSeconds` must exceed
   `leaseSeconds`**, and the template checks it.
3. **Leases are short (300 s).** A task runs for 70–170 s; a long lease would delay takeover
   past the life of the Pod.

The grace period must cover the worst preStop path, `drainDeadlineSeconds + lingerSeconds`;
the template checks that too.

Not covered: power loss, an OOM kill of the whole Pod, or `--force --grace-period=0`. A task in
flight then stays `running` briefly, and is bounded by `server.config.maxAttempts` (submissions
to SGLang, default 5, then `TooManyAttempts`) and `server.config.queueTimeoutSeconds` (the
task's total lifetime, default 86400 s).

The cost: with work in flight, each Pod takes `remaining generation time + linger` to exit,
and rolling updates are correspondingly slower.

## Monitoring

The Server exposes two Prometheus endpoints:

| Endpoint | Content |
| --- | --- |
| `/metrics` | Kept stable for existing dashboards: `h3_tasks{status}`, tasks created in the last 24 h and currently in that state |
| `/metrics/queue` | Live queue and backend gauges, all named `h3_queue_*`; this is what the ServiceMonitor scrapes |

| Metric | Labels | Meaning |
| --- | --- | --- |
| `h3_queue_tasks` | `status`, `capability` | Tasks per state. **Each Pod reports only its own capability**, so the Prometheus `service` label maps 1:1 to a pool |
| `h3_queue_oldest_queued_age_seconds` | — | Age of the oldest queued task |
| `h3_queue_oldest_running_age_seconds` | — | Age of the oldest running task; keeps climbing for a stuck one |
| `h3_queue_waiting_takeover` | — | Running tasks with an expired or empty lease, waiting for takeover; sustained `> 0` means every replica of that capability is down or stuck |
| `h3_queue_backend_unavailable` | — | Running tasks currently cut off from SGLang and retrying within their lease; sustained `> 0` means an SGLang problem |
| `h3_queue_running_on_instance` | `h3_instance` | Tasks this replica is executing |
| `h3_queue_completed_24h` | `status` | Tasks completed in the last 24 h |
| `h3_queue_backend_up` | `capability` | Whether this Pod's SGLang is reachable |
| `h3_queue_backend_jobs` | `capability`, `state` | Jobs on this Pod's SGLang, see below |
| `h3_queue_build_info` | `capability`, `h3_instance` | Always 1 |

### Label values

| Label | Values | Notes |
| --- | --- | --- |
| `status` | `queued` \| `running` \| `succeeded` \| `failed` \| `cancelled` | `queued`/`running` are backlog, the rest terminal. There is no `completed`: that is a `state` value |
| `capability` | `fl2va` \| `ref2va` | `fl2va` = text-to-video and first/last-frame image-to-video, `ref2va` = reference-media generation |
| `state` | `queued` \| `completed` \| `failed` | SGLang's own job states, not H3 task states |
| `h3_instance` | Server Pod name | |
| `service` | `<release>-server-<pool>` | Added by Prometheus at scrape time. The ServiceMonitor selects only headless Services labelled `minimax-h3/scrape=true`, so each Pod is scraped once. Use the full name in queries |

> For autoscaling use the backlog, `h3_queue_tasks{status=~"queued|running"}`, per pool with
> `max by (service) (...)`. `waiting_takeover` and `backend_unavailable` are failure alerts, not
> scaling signals.
>
> ⚠️ **An idle pool has no series, not a zero.** With `queued=0` and `running=0` its
> `h3_queue_tasks` rows do not exist, and HPA/KEDA read that as "no data". Fall back explicitly:
>
> ```promql
> max by (service) (h3_queue_tasks{status=~"queued|running"}) or vector(0)
> ```
>
> To tell an idle pool from a dead one, use `h3_queue_backend_up` or `h3_queue_build_info`, which
> exist whenever a Pod is up.

### What `h3_queue_backend_jobs` counts

**SGLang jobs, not H3 tasks**: the Server calls `GET /v1/videos` on its Pod's SGLang (SGLang
exposes no `/metrics` and no queue depth in `/stats`) and counts the jobs by `status`. The result
is cached for 5 s per Pod; with the backend unreachable only `h3_queue_backend_up 0` is reported.

| | `h3_queue_tasks` | `h3_queue_backend_jobs` |
| --- | --- | --- |
| Source | the PostgreSQL task table | this Pod's SGLang `GET /v1/videos` |
| Scope | **per capability**; replicas of a pool report the same value (use `max`, never `sum`) | **this Pod only** (`sum` is fine) |
| States | H3 task states | SGLang job states |

A task that is `running` in H3 has been submitted to SGLang, which runs one job at a time, so it
may still wait in SGLang's queue (`state="queued"`):

```promql
sum(h3_queue_backend_jobs{state="queued"})                          # backend backlog; sustained > 1 = add replicas
max(h3_queue_tasks{status="running"}) - sum(h3_queue_backend_jobs{state="queued"})   # actually on a GPU
```

High `h3_queue_tasks{status="queued"}` without a backend backlog means too few Server workers;
a backend backlog means too few GPUs. `/v1/videos` returns every job since SGLang started, so
`state="completed"` only grows: **only `state="queued"` is meaningful.**

### Two more pitfalls

- **The label is `h3_instance`, not `instance`**: Prometheus attaches its own `instance`
  (`IP:port`), `job`, `namespace` and `pod`, and would rename a clashing label to `exported_instance`.
- **Apart from `h3_queue_running_on_instance` and the two backend series, values are
  cluster-wide**: every replica reads the same database. Aggregate with `max()`; `sum()`
  multiplies by the replica count.

### Scraping

The ServiceMonitor needs the Prometheus Operator CRDs (e.g. kube-prometheus-stack):

```yaml
serviceMonitor:
  enabled: true
  labels:
    release: kube-prometheus-stack   # whatever your Prometheus's serviceMonitorSelector matches
```

**Do not drop the label.** kube-prometheus-stack selects ServiceMonitors by its release label;
without a match the object is created, looks fine in `kubectl get`, and is silently never
scraped. Find the selector, then check that targets really exist rather than just the object:

```bash
kubectl get prometheuses -A -o custom-columns=NS:.metadata.namespace,SEL:.spec.serviceMonitorSelector
kubectl -n monitoring port-forward svc/kube-prometheus-stack-prometheus 19090:9090 &
curl -s 'http://localhost:19090/api/v1/targets?state=active' | grep -c minimax
curl -s 'http://localhost:19090/api/v1/query?query=h3_queue_tasks'
```

## Ref2VA (Turbo LoRA on by default)

Both pools are on by default. To serve one capability only, disable the other (at least one
must stay, or the render fails):

```yaml
pools:
  ref2va:
    enabled: false     # T2V/I2V only
```

The Ref2VA pool runs `lmsysorg/sglang:v0.5.19`. Reference media can be combined with
`first_frame` and `last_frame` images (at most one per frame role); such requests stay in the
Ref2VA pool and need SGLang v0.5.19 or newer. The Router points `H3_REF2VA_SERVICE_URLS` at
this pool automatically.

Ref2VA loads the LightX2V 8-step 768p LoRA by default. Download it on **every node that can run
Ref2VA**:

```bash
hf download lightx2v/Minimax-h3-Turbo \
  minimax_h3_ref2v_turbo_8step_v1.0_768p_bf16.safetensors \
  --local-dir /data/models/Minimax-h3-Turbo
```

`pools.ref2va.turbo` is the only place Turbo is configured:

```yaml
pools:
  ref2va:
    turbo:
      enabled: true
      lora:
        hostPath: /data/models/Minimax-h3-Turbo
        weightName: minimax_h3_ref2v_turbo_8step_v1.0_768p_bf16.safetensors
        scale: 1.0
        alpha: 8
      sampling:
        numInferenceSteps: 9
        flowShift: 6.0
        audioFlowShift: 3.0
```

From this switch the template renders the `--lora-*` arguments (`--lora-nickname h3-ref2va-turbo`
and `--lora-merge-mode auto` are fixed), a read-only `/lora` mount, and three Server environment
variables. The base Ref2VA weights stay mounted at `/model`. A missing directory or file keeps the
replica from starting; there is no silent fallback to the base model. If only some nodes carry the
LoRA, label them and set `nodeSelector` on the Ref2VA pool.

The sampling settings `9 / 6.0 / 3.0` go to the Ref2VA Server only. SGLang v0.5.19 counts the
trailing zero sigma as a step, so `9` means eight denoiser evaluations. Keep the LoRA and sampling
identical across Ref2VA replicas, and recheck the step convention when upgrading SGLang. FL2VA
keeps its image, parallelism and 50-step sampling. The API does not change and the public model
name stays `MiniMax-H3`.

To switch, pause new Ref2VA requests, drain queued and running tasks, update the whole pool and
resume. Base mode is one switch:

```yaml
pools:
  ref2va:
    turbo:
      enabled: false
```

Without Turbo the LoRA mount and sampling variables are gone, and Ref2VA requests omit
`num_inference_steps`, `flow_shift`, `audio_flow_shift` and `quality`, leaving SGLang's base
defaults. `--warmup-mode request --warmup-steps 2` stays in both modes: H3 needs both ends of
the sigma interval, so SGLang's default one-point warmup is invalid.

The chart owns all of these. The same keys in `extraArgs`, `extraEnv` or
`extraVolumes`/`extraVolumeMounts`, or the old `server.config.ref2va*` keys, fail the render
with a pointer to the setting that replaces them.

Turbo does not work with an external SGLang: `turbo.enabled: true` together with
`sglang.enabled: false` is refused. Base mode can use an external backend.

**Server image protocol.** In both Turbo states, Ref2VA Pods first run the `ref2va-config-check`
init container from **the same Server image**, which checks that the image declares Ref2VA config
protocol 1; an older image stops at initialization with an explicit error. FL2VA does not run it.

**Weights are on hostPath: label nodes, do not pin hostnames.** When only some machines carry the
weights, label them and select on the label, so scheduling and capacity stay with Kubernetes
(pinning a hostname leaves the Pod Pending when that node has no free GPUs):

```bash
kubectl label node <node> minimax-h3/model=present
```

```yaml
pools:
  ref2va:
    sglang:
      nodeSelector: {minimax-h3/model: present}
```

## Exposing through openresty (optional)

With [autoconfig](https://github.com/modelsphere/autoconfig) and
[llm-openresty](https://github.com/modelsphere/llm-openresty) in the cluster, the chart can
publish itself as a **video ModelRoute**; autoconfig renders the openresty route and reloads it,
and the service is reachable at `https://<host>/<modelRoute.route>/v2/...`:

```yaml
modelRoute:
  enabled: true
  route: minimax-h3
  openrestyService: <ns>/openresty
  outputConfigMap: <ns>/openresty-conf
server:
  config:
    publicBaseUrl: https://<host>/minimax-h3   # openresty strips /<route>/; H3 adds it back to download URLs
```

The ModelRoute lives in the release's namespace (its references to openresty are `ns/name`), so it
is removed with the release and never points at a backend that is gone. Without autoconfig keep
`modelRoute.enabled: false`, the default; the render refuses a ModelRoute on a cluster that does
not serve the CRD.

| Value | Default | Notes |
| --- | --- | --- |
| `modelRoute.route` | `minimax-h3` | External path prefix. Change the tail of `server.config.publicBaseUrl` with it, or download links break |
| `modelRoute.values.rate_limit` | `200Mbps` | **Total** download bandwidth across all download connections. openresty is shared with other models, and a few concurrent video downloads can fill the NIC |
| `modelRoute.values.download_conn_limit` | `"20"` | Concurrent downloads across all clients; `rate_limit` is split evenly between them. Quote it: the CRD map is string-typed |
| `modelRoute.values.max_body_size` | `64m` | Request body limit |
| `modelRoute.values.auth` | on | Bearer API keys from openresty's key file; `auth: "false"` turns it off. `auth_public_paths` defaults to the download path, since `content.url` is handed to end users |

## Scheduling

| Value | Default | Notes |
| --- | --- | --- |
| `sglang.schedulerName` | `""` | Set it to the scheduler your other GPU workloads use (e.g. `volcano`): two schedulers placing Pods on the same GPUs can double-book them. H3 needs no gang scheduling; each Pod is a self-contained 4-GPU unit |
| `sglang.priorityClassName` | `""` | Without one the Pods run at priority 0 and lose GPUs to higher-priority workloads |
| `router.priorityClassName` | `""` | The Router is the entry point; healthy backends are useless if it is preempted |
| `sglang.tolerations` | `nvidia.com/gpu` (any value) `NoSchedule` | For GPU nodes tainted for compute. The Router carries no toleration and stays on CPU nodes |
| `sglang.gpuProduct` | `[]` | Restrict to GPU models (`nvidia.com/gpu.product` values); `sglang.affinity`, when set, wins |
| `sglang.resources.*.ephemeral-storage` | `16Gi` | Local disk quota; without it the scheduler does not account for it and a full disk triggers DiskPressure eviction |

Mixing GPU models in one pool makes latency unpredictable: the same 768P/4 s generation takes
about 2.5× longer on an A100 than on an H800. Split pools by model (one `gpuProduct` each) or
accept the long tail.

## Values worth knowing

`values.yaml` documents every block. The ones that bite:

| Value | Default | Notes |
| --- | --- | --- |
| `database.url` | `""` | Required unless `database.existingSecret` or `database.fromSecret.{namespace,name}` is set |
| `sharedStorage.storageClass` | `""` | Must be RWX; empty uses the cluster default class |
| `server.apiService.type` | `ClusterIP` | `NodePort` for direct access without the Router |
| `sglang.shmSize` | `64Gi` | `/dev/shm` as `emptyDir{medium: Memory}`; **counts against the container memory limit** |
| `sglang.resources.limits.memory` | `512Gi` | Loading peaks at roughly 350–380 GiB of host memory (steady state ~19 GiB); 256Gi gets OOM-killed. Lower `shmSize` with it on smaller nodes |
| `server.config.allowPrivateUrls` | `false` | Allow private-network image and callback URLs |
| `server.config.backendReadyTimeoutSeconds` | `2` | Timeout of the SGLang `/health` check before a worker claims a task; tasks wait in the queue while SGLang is not ready |
| `server.config.queueTimeoutSeconds` | `86400` | Upper bound on a task's **total** lifetime (queued, running, resubmitted, downloading). A shorter bound frees a stuck replica sooner but fails tasks that are merely slow |
| `server.config.maxAttempts` | `5` | Submissions to SGLang per task, the first included; then the task fails with `TooManyAttempts`. Failed status queries do not count |

## Checking a render

```bash
helm lint ./charts/minimax-h3 --set database.url=postgresql://u:p@db:5432/minimaxh3
helm template h3 ./charts/minimax-h3 -n minimax-h3 --set database.url=postgresql://u:p@db:5432/minimaxh3 \
  | kubectl apply --dry-run=server -f -
helm unittest ./charts/minimax-h3
python3 ./charts/minimax-h3/test-ref2va-config-check.py
```
