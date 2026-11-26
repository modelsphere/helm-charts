# higress-llm

The gateway-wide plugin config that makes a [Higress](https://higress.io) gateway an
OpenAI-compatible entry in front of OpenResty. It renders no workload: five
`WasmPlugin` objects and one `EnvoyFilter` for a gateway installed by the upstream
`higress-core` chart (`https://higress.io/helm-charts`), in the same namespace.

| Plugin | Does |
|---|---|
| `model-router` | reads `model` from the body into `x-higress-llm-model`, which the fleet Ingresses match |
| `key-auth` | checks the caller's `Authorization: Bearer <key>` and the route's allow list (`FAIL_CLOSE`) |
| `ai-statistics` | token metrics and `ai_log` per route, model and caller |
| `ai-proxy` | replaces the caller's key with the one OpenResty accepts |
| `ai-backend-router` | picks a backend by tier and weight; with `backendRouter.spill`, the `EnvoyFilter` replays a request that got 429/502/503/504 on the next tier |

**Nothing per model or per caller is configured here.** Routes come from `ModelFleet`
objects and callers from `APIConsumer` objects (`routing.modelsphere.dev/v1alpha1`),
which [autoconfig](../autoconfig) writes into the Ingresses and the plugins'
`matchRules` and into key-auth's `defaultConfig.consumers`. It needs an autoconfig
release with the ModelFleet and APIConsumer controllers.

## Render it against the cluster, never offline

key-auth refuses a config without a consumer list and fails closed, and under
server-side apply that list has one owner: autoconfig. So the chart looks at the live
key-auth before rendering (`lookup`):

| Live key-auth | Rendered `consumers` |
|---|---|
| none | `[]` |
| list owned by helm alone, empty | `[]` |
| list owned by any other manager (autoconfig) | left out |
| list owned by helm alone, non-empty | refuses to render |

Without a cluster connection `lookup` returns nothing, so an offline render takes the
first row. **Do not install or sync this chart through anything that renders it
offline and applies the result** (`helm template | kubectl apply`, Argo CD, which
renders with `helm template`, kustomize's `helmCharts`): that writes `consumers: []` over autoconfig's list, and every
route with an allow list answers 401 to everyone until autoconfig's next resync.
Use `helm install/upgrade` (or `helm template --dry-run=server`) with access to the
cluster. `helm template` and `helm diff` without it show `consumers: []` even when an
upgrade will leave the list out.

Each key may belong to one consumer only: key-auth rejects the whole config on a
duplicate credential.

## Values

| Value | Default | |
|---|---|---|
| `globalAuth` | `false` | `false`: key-auth checks only routes that have an allow list; `true`: every route needs a known key |
| `backendRouter.spill` | `true` | replay to the next tier; buffers request bodies on every route of the gateway |
| `backendRouter.maxRequestBodyBytes` | `16777216` | buffer limit (larger requests get 413); the chart refuses less than ai-backend-router's own 16 MiB |
| `plugins.*` | see `values.yaml` | plugin image URLs; the gateway pods pull them themselves |
