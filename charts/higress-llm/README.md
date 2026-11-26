# higress-llm

The gateway-wide plugin config that makes a [Higress](https://higress.io) gateway an
OpenAI-compatible entry in front of OpenResty. It renders no workload: six
`WasmPlugin` objects and one `EnvoyFilter` for a gateway installed by the upstream
`higress-core` chart (`https://higress.io/helm-charts`), in the same namespace.

| Plugin | Does |
|---|---|
| `model-router` | reads `model` from the body into `x-higress-llm-model`, which the fleet Ingresses match |
| `key-auth` | checks the caller's `Authorization: Bearer <key>` and the route's allow list (`FAIL_CLOSE`) |
| `ai-statistics` | token metrics and `ai_log` per route, model and caller |
| `ai-proxy` | replaces the caller's key with the one OpenResty accepts |
| `ai-backend-router` | picks a backend by tier and weight; with `backendRouter.spill`, the `EnvoyFilter` replays a request that got 429/502/503/504 on the next tier |
| `ai-stream-guard` | cuts a streamed answer stuck in a repetition loop (`finish_reason: content_filter`), on fleet routes |

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
empty list when it is missing (the chart labels key-auth
`routing.modelsphere.dev/consumers: autoconfig`, which is what tells autoconfig the
field is its to write, in whatever namespace the chart is installed). **This needs autoconfig 0.4.4 or later, installed
first**: an earlier release never adds the list, and the gateway stays closed. Install
in the order higress-core, autoconfig, this chart: with the WasmPlugin CRD in place
when autoconfig starts, it adds the list once key-auth appears: measured about 5 s for
the first key-auth after autoconfig starts, about 0.3 s after that (an autoconfig
started before Higress picks it up on its next periodic pass). Between this chart
creating key-auth and autoconfig adding the list, key-auth rejects every request on
the gateway: harmless on a new gateway, an outage on one that already serves other
traffic.

Each key may belong to one consumer only: key-auth rejects the whole config on a
duplicate credential.

## ai-stream-guard

On by default on every fleet route: autoconfig adds a rule per fleet (a fleet opts out
with `spec.gateway.streamGuard.disabled: true`) carrying a copy of this chart's
`streamGuard`, rendered as the plugin's `defaultConfig`. With `defaultConfigDisable`
the gateway is never sent `defaultConfig` (which keeps the detectors off every other
route), so a rule must carry the detectors itself: on a rule without them the plugin
fails to load, and the gateway keeps running the module it had before. The chart
refuses an empty `streamGuard.detectors`. On a cut the client gets the chunks up to the trigger,
a terminal chunk with `finish_reason: content_filter` and `[DONE]`, and no usage
chunk. `/v1/messages` clients get `stop_reason: "content_filter"` and a
`message_delta` without `usage`, which the Anthropic TypeScript SDK's `MessageStream`
reports as an error. A cut happens after the first byte, so it is never spilled to
another tier. The plugin is `FAIL_OPEN`: if it cannot load, streams pass uncut.

**A cut stream is not billed.** The usage chunk never arrives (the gateway stops reading
the upstream at the cut), so `ai-statistics` records no tokens and the `ai_log` of the
request has no `input_token`/`output_token`. An OpenResty bodylog behind the gateway has
no tokens either, and no `finish_reason`. The way to find cuts is `ai_log`'s
`stream_guard_action: cut` (with `stream_guard_tail_chunks` and friends) or the
gateway's `ai-stream-guard: cut stream` warn line; the bodylog has no marker. See
the plugin's [README](https://github.com/modelsphere/higress/tree/main/plugins/wasm-go/extensions/ai-stream-guard)
for the detectors and the `ai_log` attributes.

## Values

| Value | Default | |
|---|---|---|
| `globalAuth` | `false` | `false`: key-auth checks only routes that have an allow list; `true`: every route needs a known key |
| `backendRouter.spill` | `true` | replay to the next tier; buffers request bodies on every route of the gateway |
| `backendRouter.maxRequestBodyBytes` | `16777216` | buffer limit (larger requests get 413); the chart refuses less than ai-backend-router's own 16 MiB |
| `streamGuard.detectors` | repetition, 512 chunks | ai-stream-guard's detectors for fleet routes; must not be empty |
| `streamGuard.cutoff` | `{}` | what a client gets on a cut (`finish_reason`, `message`) |
| `plugins.*` | see `values.yaml` | plugin image URLs; the gateway pods pull them themselves |
