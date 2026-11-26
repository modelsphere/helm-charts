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
`matchRules` and into key-auth's `defaultConfig.consumers`. It needs autoconfig 0.4.4
or later (the ModelFleet and APIConsumer controllers).

## key-auth's consumer list belongs to autoconfig

The chart renders key-auth without `defaultConfig.consumers`. The list has one owner
under server-side apply, autoconfig, so a value rendered here would be written over
autoconfig's list on every upgrade. The chart therefore renders the same thing online
and offline (`helm template`, Argo CD).

key-auth refuses a config without the list and fails closed, so autoconfig adds an
empty list when it is missing. **This needs autoconfig 0.4.4 or later, installed
first**: an earlier release never adds the list, and the gateway stays closed. Between this chart
creating key-auth and autoconfig adding the list, key-auth rejects every request on
the gateway: harmless on a new gateway, an outage on one that already serves other
traffic.

Each key may belong to one consumer only: key-auth rejects the whole config on a
duplicate credential.

## Values

| Value | Default | |
|---|---|---|
| `globalAuth` | `false` | `false`: key-auth checks only routes that have an allow list; `true`: every route needs a known key |
| `backendRouter.spill` | `true` | replay to the next tier; buffers request bodies on every route of the gateway |
| `backendRouter.maxRequestBodyBytes` | `16777216` | buffer limit (larger requests get 413); the chart refuses less than ai-backend-router's own 16 MiB |
| `plugins.*` | see `values.yaml` | plugin image URLs; the gateway pods pull them themselves |
