#!/usr/bin/env bash
# End-to-end test for the rdma-injector chart: helm install, then --dry-run=server
# (which goes through the real webhook without persisting anything) to check that
# a pod labelled rdma-ib:"true" comes back with the /etc/gpu-node mount and the
# sourcing snippet, and that an unlabelled one does not. Uninstalls afterwards.
#
# Run it where kubectl works. Usage:
#   HELM=/path/to/helm CHART=./rdma-injector bash test-inject.sh
set -uo pipefail
HELM=${HELM:-helm}
CHART=${CHART:-/tmp/rdma-injector}
NS=${NS:-kube-system}          # any namespace works; the certificate SAN follows it

echo "===== 1) helm install (ns=$NS) ====="
$HELM upgrade --install rdma-injector "$CHART" -n "$NS" --create-namespace 2>&1 | tail -6
echo "===== 2) waiting for the webhook to become ready ====="
kubectl -n "$NS" rollout status deploy/rdma-injector --timeout=90s
kubectl -n "$NS" get pods -l app=rdma-injector -o wide --no-headers | awk '{print "  pod:",$1,$2,$3,$7}'

echo "===== 3) a labelled pod should be injected (--dry-run=server hits the real webhook) ====="
cat > /tmp/pod-labeled.yaml <<POD
apiVersion: v1
kind: Pod
metadata: { name: rdma-test-labeled, namespace: default, labels: { rdma-ib: "true" } }
spec:
  containers:
  - name: c
    image: python:3.12-alpine
    command: ["sh","-c"]
    args: ["echo hi; sleep 1"]
POD
kubectl create -f /tmp/pod-labeled.yaml --dry-run=server -o yaml > /tmp/mutated.yaml 2>&1
grep -q "name: gpunode"   /tmp/mutated.yaml && echo "  OK   gpunode volume injected"       || echo "  FAIL no gpunode volume"
grep -q "/etc/gpu-node"   /tmp/mutated.yaml && echo "  OK   /etc/gpu-node mount injected"  || echo "  FAIL no mount"
grep -q "nccl-ib.env"     /tmp/mutated.yaml && echo "  OK   sourcing snippet injected"     || echo "  FAIL no snippet"

echo "===== 4) an unlabelled pod should NOT be injected (control) ====="
sed 's/rdma-test-labeled/rdma-test-plain/; /rdma-ib/d' /tmp/pod-labeled.yaml > /tmp/pod-plain.yaml
kubectl create -f /tmp/pod-plain.yaml --dry-run=server -o yaml 2>&1 | grep -q "gpunode" \
  && echo "  FAIL injected when it should not have been" || echo "  OK   not injected, as expected"

echo "===== 5) cleanup ====="
$HELM uninstall rdma-injector -n "$NS" 2>&1 | tail -1
