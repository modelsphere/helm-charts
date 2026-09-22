# rdma-injector

A mutating admission webhook that wires pods labelled `rdma-ib: "true"` into the
node's RDMA environment, so workload manifests stay free of the boilerplate:

- a read-only hostPath mount of `/etc/gpu-node`, and
- a snippet prepended to the container's start-up script that runs
  `source /etc/gpu-node/nccl-ib.env`, putting that node's healthy InfiniBand
  ports into `NCCL_IB_HCA`.

The logic is in [`image/inject_webhook.py`](image/inject_webhook.py) and uses
nothing outside the Python standard library.

## Layout

```
rdma-injector/
  Chart.yaml  values.yaml  README.md  test-inject.sh
  image/
    Dockerfile  inject_webhook.py     # code only -- no certificate is baked in
  templates/
    deployment.yaml   # mounts the Secret Helm generates at /tls
    service.yaml
    webhook.yaml      # self-signed cert -> Secret + MutatingWebhookConfiguration
```

## Install

```bash
helm install rdma-injector modelsphere/rdma-injector -n kube-system
```

Any namespace works -- the certificate's SAN follows `Release.Namespace`:

```bash
helm install rdma-injector modelsphere/rdma-injector -n rdma-system --create-namespace
helm upgrade rdma-injector modelsphere/rdma-injector -n rdma-system
```

Then label any pod outside the webhook's own namespace with `rdma-ib: "true"`
and it is injected on creation.

## Building the image

Only needed if you change the code.

```bash
cd image
docker build -t 4pdosc/rdma-injector:0.2.0 .
docker push 4pdosc/rdma-injector:0.2.0
```

## End-to-end test

```bash
HELM=helm CHART=./rdma-injector bash test-inject.sh
```

Installs the chart, uses `--dry-run=server` (which goes through the real webhook
without persisting anything) to confirm a labelled pod is injected and an
unlabelled one is not, then uninstalls.

## Certificates

Helm self-signs the serving certificate with `genSignedCert`, SAN
`rdma-injector.<namespace>.svc`, and fills in the matching `caBundle`. The
apiserver validates the webhook against that CA, so the SAN has to match the
Service's DNS name -- deriving it from `Release.Namespace` is what makes the
chart namespace-agnostic.

Validity is `certValidityDays` (3650 by default). Upgrades look the existing
Secret up and reuse it, so the certificate does not churn; to force a new one,
delete the `rdma-injector-certs` Secret and upgrade again.

## Design notes

**`failurePolicy: Fail`.** If the webhook is unreachable, creation of a labelled
pod is rejected rather than silently admitted without RDMA — hence `replicas: 2`.
It cannot deadlock itself: the webhook carries no `rdma-ib` label, and its own
namespace is excluded by `namespaceSelector`.

**Why the snippet instead of the value.** Admission happens before scheduling,
so the webhook does not know which node the pod will land on and cannot know
that node's `NCCL_IB_HCA`. It injects the plumbing to resolve it on the node.
