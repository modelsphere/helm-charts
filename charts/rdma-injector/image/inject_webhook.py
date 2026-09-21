#!/usr/bin/env python3
# rdma-injector —— Mutating Admission Webhook
#
# Wires pods labelled `rdma-ib: "true"` into the node's RDMA environment, so that
# workload manifests stay free of the hostPath mount and the `source` line that
# would otherwise have to be repeated in every one of them.
#
# What it injects (idempotently -- anything already present is left alone):
#   1) a volume `gpunode`, hostPath /etc/gpu-node, where the node's list of
#      healthy InfiniBand ports is written by the node preparation script;
#   2) on every container, a read-only volumeMount of it at /etc/gpu-node;
#   3) on every container started through a shell (bash/sh with -c or -lc), a
#      snippet prepended to the start-up script that sources
#      /etc/gpu-node/nccl-ib.env, putting NCCL_IB_HCA into the environment so
#      NCCL only uses the ports that are healthy on that node.
#
# Why source it at container start rather than have the webhook fill the value
# in: admission happens before scheduling, so the webhook does not yet know
# which node the pod will land on and cannot know that node's NCCL_IB_HCA. What
# it injects is the plumbing to resolve it once the pod is there.
#
# Runs as a small HTTP server on :8443 with the TLS certificate mounted from a
# Secret at /tls. Only the Python standard library is used.
import json, base64, ssl, os
from http.server import BaseHTTPRequestHandler, HTTPServer

GPU_NODE_DIR = "/etc/gpu-node"
ENV_FILE     = GPU_NODE_DIR + "/nccl-ib.env"
VOL_NAME     = "gpunode"
SNIPPET      = "[ -f %s ] && set -a && . %s && set +a; " % (ENV_FILE, ENV_FILE)


def build_patch(pod):
    spec = pod.get("spec", {}) or {}
    patch = []

    # 1) the volume (creating the whole array if the pod has none)
    vols = spec.get("volumes")
    vol = {"name": VOL_NAME, "hostPath": {"path": GPU_NODE_DIR, "type": "Directory"}}
    if vols is None:
        patch.append({"op": "add", "path": "/spec/volumes", "value": [vol]})
    elif not any(v.get("name") == VOL_NAME for v in vols):
        patch.append({"op": "add", "path": "/spec/volumes/-", "value": vol})

    # 2) per container: the volumeMount, and 3) the sourcing snippet
    for i, c in enumerate(spec.get("containers", []) or []):
        mounts = c.get("volumeMounts")
        mnt = {"name": VOL_NAME, "mountPath": GPU_NODE_DIR, "readOnly": True}
        if mounts is None:
            patch.append({"op": "add", "path": "/spec/containers/%d/volumeMounts" % i, "value": [mnt]})
        elif not any(m.get("name") == VOL_NAME for m in mounts):
            patch.append({"op": "add", "path": "/spec/containers/%d/volumeMounts/-" % i, "value": mnt})

        cmd = c.get("command", []) or []
        args = c.get("args", []) or []
        is_shell = len(cmd) >= 2 and os.path.basename(cmd[0]) in ("bash", "sh") \
            and any(f in cmd for f in ("-c", "-lc"))
        if is_shell and args:
            j = len(args) - 1                      # the script body is normally the last arg
            if SNIPPET not in args[j]:
                patch.append({"op": "replace",
                              "path": "/spec/containers/%d/args/%d" % (i, j),
                              "value": SNIPPET + args[j]})
    return patch


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n))
        except Exception:
            self.send_response(400); self.end_headers(); return
        req = body.get("request", {}) or {}
        pod = req.get("object", {}) or {}
        try:
            patch = build_patch(pod)
        except Exception:
            patch = []                              # admit on error: never block pod creation
        resp = {"apiVersion": body.get("apiVersion", "admission.k8s.io/v1"),
                "kind": "AdmissionReview",
                "response": {"uid": req.get("uid", ""), "allowed": True}}
        if patch:
            resp["response"]["patchType"] = "JSONPatch"
            resp["response"]["patch"] = base64.b64encode(json.dumps(patch).encode()).decode()
        out = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(out)

    def do_GET(self):                               # /healthz
        self.send_response(200); self.end_headers(); self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    httpd = HTTPServer(("0.0.0.0", 8443), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain("/tls/tls.crt", "/tls/tls.key")
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    print("rdma-injector webhook serving on :8443", flush=True)
    httpd.serve_forever()
