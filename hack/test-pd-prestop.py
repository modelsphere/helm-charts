#!/usr/bin/env python3
"""
Behavioral tests for the PD DisaggregatedSet engine preStop hook rendered by
charts/sglang and charts/vllm.

Renders the prefill leader preStop Python script with `helm template` and runs
it against in-process localhost mock routers + engine metrics/healthz servers
with stubbed DNS and URL rewriting, verifying:
  1. Fast path: DELETE /workers/{id} (sglang) / POST /remove_worker (vllm)
     evicts this pod from all Ready routers and proceeds cleanly to drain.
  2. Fallback path: remove call returns 401 (or silently no-ops on @0 DP rank
     for vllm) -> loop keeps polling GET /workers until both routers restart
     without this pod, then drains inflight == 0.
  3. Router-2-in-preStop race: len(routers) < router.replicas holds wait even
     when the remaining router has already dropped this pod, until all routers
     are Ready again.
  4. Atomic rollout / 1->0 escape hatch: no surviving peers in *-hl-s* ->
     skips router wait.
  5. Last-worker escape hatch: router lists only 1 worker of role -> does not
     send DELETE/remove_worker and skips wait.

Zero third-party dependencies (Python 3 stdlib only). Requires `helm`.
"""

import http.server
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request


def render_prestop(chart_path: str, engine_port: int, router_port: int) -> str:
    cmd = [
        "helm",
        "template",
        "ci",
        chart_path,
        "-n",
        "ci",
        "--show-only",
        "templates/pd-ds.yaml",
        "--set",
        "modelRoute.nginx.outputConfigMap=ci/openresty-conf",
        "--set",
        "pd.enabled=true",
        "--set",
        "rdma.enabled=true",
        "--set",
        "scaler.enabled=false",
        "--set",
        "pd.prefill.gpus=4",
        "--set",
        "pd.decode.gpus=4",
        "--set",
        f"pd.prefill.httpPort={engine_port}",
        "--set",
        f"service.port={router_port}",
        "--set",
        "hangWatcher.enabled=false",
        "--set",
        "lifecycle.preStop.endpointSyncSeconds=0",
        "--set",
        "lifecycle.preStop.pollIntervalSeconds=1",
        "--set",
        "lifecycle.preStop.drainSeconds=20",
        "--set",
        "lifecycle.shutdownReserveSeconds=1",
    ]
    if os.path.basename(os.path.abspath(chart_path)) == "vllm":
        cmd.extend(["--set", "lifecycle.shutdownTimeout=1"])
    yaml_text = subprocess.check_output(cmd, text=True)
    # Extract the DisaggregatedSet prefill leader preStop python3 -c block.
    # Sprig `dict` renders "decode" alphabetically before "prefill", so seek to
    # DisaggregatedSet -> "- name: prefill" before searching for the script.
    lines = yaml_text.splitlines()
    start_idx = 0
    in_ds = False
    for idx, line in enumerate(lines):
        if line.strip() == "kind: DisaggregatedSet":
            in_ds = True
        elif in_ds and line.strip() == "- name: prefill":
            start_idx = idx
            break
    else:
        raise RuntimeError(f"Could not find DisaggregatedSet prefill role in {chart_path}")

    for i in range(start_idx, len(lines) - 2):
        if lines[i].strip() == "- python3" and lines[i + 1].strip() == "- -c":
            j = i + 2
            while j < len(lines) and (not lines[j].strip() or lines[j].strip().startswith("#")):
                j += 1
            if j < len(lines) and lines[j].strip() == "- |":
                block = []
                base_indent = None
                for line in lines[j + 1 :]:
                    if not line.strip():
                        block.append("")
                        continue
                    indent = len(line) - len(line.lstrip(" "))
                    if base_indent is None:
                        base_indent = indent
                    if indent < base_indent:
                        break
                    block.append(line[base_indent:])
                script = "\n".join(block).strip() + "\n"
                assert 'PD_ROLE = "prefill"' in script, "extracted wrong role preStop"
                return script
    raise RuntimeError(f"Could not find python3 preStop block for prefill in {chart_path}")


class MockState:
    def __init__(self, chart_name: str, my_ip: str, engine_port: int, router_port: int) -> None:
        self.lock = threading.Lock()
        self.chart_name = chart_name
        self.my_ip = my_ip
        self.engine_port = engine_port
        self.router_port = router_port
        self.metric_name = (
            "sglang:num_running_reqs" if chart_name == "sglang" else "vllm:num_requests_running"
        )
        self.inflight = 0
        self.dns: dict[str, list[str]] = {}
        # routers: rip -> list of worker dicts {"id", "url", "worker_type"}
        self.routers: dict[str, list[dict]] = {}
        self.remove_status = 200
        self.remove_noop = False
        self.remove_calls: list[tuple[str, str]] = []
        self.tick_cb = None
        self.sleep_count = 0


def start_mock_server(state: MockState, router_ports: dict[str, int], engine_port_map: list[int]):
    """Starts HTTP servers on 127.0.0.1 for router 1, router 2, and the engine."""

    def make_router_handler(rip: str):
        class RouterHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return

            def do_GET(self):
                if self.path == "/workers":
                    with state.lock:
                        workers = list(state.routers.get(rip, []))
                    body = json.dumps({"workers": workers, "total": len(workers)}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(404)
                    self.end_headers()

            def do_DELETE(self):
                parsed = urllib.parse.urlsplit(self.path)
                wid = parsed.path.rsplit("/", 1)[-1]
                with state.lock:
                    state.remove_calls.append(("DELETE", f"{rip}:{parsed.path}"))
                    code = state.remove_status
                    noop = state.remove_noop
                    if code in (200, 202) and not noop:
                        state.routers[rip] = [w for w in state.routers[rip] if w.get("id") != wid]
                self.send_response(code)
                self.end_headers()

            def do_POST(self):
                parsed = urllib.parse.urlsplit(self.path)
                qs = urllib.parse.parse_qs(parsed.query)
                target = (qs.get("url") or [""])[0]
                with state.lock:
                    state.remove_calls.append(("POST", f"{rip}:{parsed.path}?url={target}"))
                    code = state.remove_status
                    noop = state.remove_noop
                    if code in (200, 202) and not noop:
                        state.routers[rip] = [w for w in state.routers[rip] if w.get("url") != target]
                self.send_response(code)
                self.end_headers()

        return RouterHandler

    class EngineHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            return

        def do_GET(self):
            if self.path == "/metrics":
                with state.lock:
                    val = state.inflight
                    name = state.metric_name
                body = f"{name} {val}\n".encode()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

    servers = []
    for rip in ("10.99.0.20", "10.99.0.21"):
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), make_router_handler(rip))
        router_ports[rip] = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        servers.append(srv)

    esrv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), EngineHandler)
    engine_port_map.append(esrv.server_address[1])
    t = threading.Thread(target=esrv.serve_forever, daemon=True)
    t.start()
    servers.append(esrv)
    return servers


def run_prestop(script: str, state: MockState) -> list[str]:
    router_ports: dict[str, int] = {}
    engine_ports: list[int] = []
    servers = start_mock_server(state, router_ports, engine_ports)
    real_engine_port = engine_ports[0]

    # Strip trailing os.kill(1, SIGTERM) just in case (kill=true isn't enabled on
    # default non-LWS DS templates unless lifecycle.preStopKill is true, wait:
    # preStopKill IS true by default! So keep os.kill mocked cleanly).
    orig_getaddrinfo = socket.getaddrinfo
    orig_open = urllib.request.OpenerDirector.open
    orig_sleep = time.sleep
    orig_kill = os.kill
    orig_environ_ip = os.environ.get("POD_IP")
    orig_environ_slice = os.environ.get("PD_SLICE")

    logs: list[str] = []
    fake_clock = [100.0]
    orig_monotonic = time.monotonic

    def fake_getaddrinfo(host, port, family=0, *args, **kwargs):
        with state.lock:
            has_stub = host in state.dns
            ips = list(state.dns.get(host, []))
        if has_stub:
            if not ips:
                raise socket.gaierror(-2, f"Name or service not known: {host}")
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, int(port or 0))) for ip in ips]
        return orig_getaddrinfo(host, port, family, *args, **kwargs)

    def fake_open(self, fullurl, data=None, timeout=socket._GLOBAL_DEFAULT_TIMEOUT):
        if isinstance(fullurl, urllib.request.Request):
            url = fullurl.full_url
            method = fullurl.get_method()
            hdrs = dict(fullurl.headers)
            req_data = fullurl.data if data is None else data
        else:
            url = str(fullurl)
            method = "GET"
            hdrs = {}
            req_data = data
        p = urllib.parse.urlsplit(url)
        host = p.hostname
        port = p.port
        if host in router_ports and port == state.router_port:
            url = urllib.parse.urlunsplit(
                (p.scheme, f"127.0.0.1:{router_ports[host]}", p.path, p.query, p.fragment)
            )
        elif host == "127.0.0.1" and port == state.engine_port:
            url = urllib.parse.urlunsplit(
                (p.scheme, f"127.0.0.1:{real_engine_port}", p.path, p.query, p.fragment)
            )
        req = urllib.request.Request(url, data=req_data, headers=hdrs, method=method)
        return orig_open(self, req, timeout=timeout)

    def fake_sleep(secs):
        fake_clock[0] += float(secs)
        with state.lock:
            state.sleep_count += 1
            cb = state.tick_cb
            sc = state.sleep_count
        if cb is not None:
            cb(state, sc)

    def fake_monotonic():
        return fake_clock[0]

    def fake_kill(pid, sig):
        logs.append(f"KILL:{pid}:{sig}")
        raise SystemExit(0)

    # Capture log() output too without relying on /proc/1/fd/1 on macOS
    wrapped_script = script + "\n"
    try:
        os.environ["POD_IP"] = state.my_ip
        os.environ["PD_SLICE"] = "0"
        socket.getaddrinfo = fake_getaddrinfo
        urllib.request.OpenerDirector.open = fake_open
        time.sleep = fake_sleep
        time.monotonic = fake_monotonic
        os.kill = fake_kill

        ns: dict = {}
        code = compile(wrapped_script, f"prestop-{state.chart_name}.py", "exec")
        # Inject a wrapper around open('/proc/1/fd/1')? Simpler: override built-in log after defining defs?
        # Even simpler: run until def log, or hook builtins.open for /proc/1/fd/1!
        import builtins

        orig_builtin_open = builtins.open

        class _LogCapture:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def write(self, s):
                logs.append(s.rstrip("\n"))

        def fake_builtin_open(file, *args, **kwargs):
            if file == "/proc/1/fd/1":
                return _LogCapture()
            return orig_builtin_open(file, *args, **kwargs)

        builtins.open = fake_builtin_open
        try:
            try:
                exec(code, ns)
            except SystemExit:
                pass
        finally:
            builtins.open = orig_builtin_open
    finally:
        socket.getaddrinfo = orig_getaddrinfo
        urllib.request.OpenerDirector.open = orig_open
        time.sleep = orig_sleep
        time.monotonic = orig_monotonic
        os.kill = orig_kill
        if orig_environ_ip is None:
            os.environ.pop("POD_IP", None)
        else:
            os.environ["POD_IP"] = orig_environ_ip
        if orig_environ_slice is None:
            os.environ.pop("PD_SLICE", None)
        else:
            os.environ["PD_SLICE"] = orig_environ_slice
        for s in servers:
            s.shutdown()
            s.server_close()
    return logs


def base_workers(my_url: str, peer_url: str, decode_url: str) -> list[dict]:
    return [
        {"id": "id-p1", "url": peer_url, "worker_type": "prefill"},
        {"id": "id-p2", "url": my_url, "worker_type": "prefill"},
        {"id": "id-d1", "url": decode_url, "worker_type": "decode"},
    ]


def run_suite_for_chart(chart_name: str, chart_path: str) -> None:
    my_ip = "10.0.0.2"
    peer_ip = "10.0.0.1"
    eport = 30001 if chart_name == "sglang" else 8001
    rport = 30000 if chart_name == "sglang" else 8000
    my_url = f"http://{my_ip}:{eport}"
    peer_url = f"http://{peer_ip}:{eport}"
    decode_url = f"http://10.0.0.9:{eport + 1}"
    ready_fqdn = "ci-prefill-hl-s0.ci.svc.cluster.local."
    router_fqdn = "ci-router-hl-s0.ci.svc.cluster.local."

    script = render_prestop(chart_path, eport, rport)

    # 1. Fast path: remove succeeds on both routers, then inflight drains 1 -> 0.
    st = MockState(chart_name, my_ip, eport, rport)
    st.inflight = 1
    st.dns = {ready_fqdn: [peer_ip], router_fqdn: ["10.99.0.20", "10.99.0.21"]}
    st.routers = {
        "10.99.0.20": base_workers(my_url, peer_url, decode_url),
        "10.99.0.21": base_workers(my_url, peer_url, decode_url),
    }

    def tick_fast(s: MockState, sc: int):
        if sc >= 3:
            s.inflight = 0

    st.tick_cb = tick_fast
    logs = run_prestop(script, st)
    assert len(st.remove_calls) == 2, f"{chart_name} fast: expected 2 remove calls, got {st.remove_calls}; logs={logs}"
    assert any("dropped http://" in l for l in logs), f"{chart_name} fast: missing dropped log: {logs}"
    assert any("drained: nothing in flight" in l for l in logs), f"{chart_name} fast: missing drain log: {logs}"
    print(f"ok    {chart_name}: fast path remove + inflight drain")

    # 2. Fallback path: remove fails 401 (or @rank noop) -> waits for routers to restart clean.
    st = MockState(chart_name, my_ip, eport, rport)
    st.remove_status = 401
    st.dns = {ready_fqdn: [peer_ip], router_fqdn: ["10.99.0.20", "10.99.0.21"]}
    dp_workers = [
        {"id": "id-p1", "url": f"{peer_url}@0", "worker_type": "prefill"},
        {"id": "id-p2", "url": f"{my_url}@0", "worker_type": "prefill"},
        {"id": "id-d1", "url": decode_url, "worker_type": "decode"},
    ]
    st.routers = {"10.99.0.20": list(dp_workers), "10.99.0.21": list(dp_workers)}

    def tick_fallback(s: MockState, sc: int):
        if sc == 3:
            s.routers["10.99.0.20"] = [w for w in s.routers["10.99.0.20"] if w["id"] != "id-p2"]
        if sc == 5:
            s.routers["10.99.0.21"] = [w for w in s.routers["10.99.0.21"] if w["id"] != "id-p2"]

    st.tick_cb = tick_fallback
    logs = run_prestop(script, st)
    assert st.sleep_count >= 5, f"{chart_name} fallback: exited too early (sleeps={st.sleep_count}); logs={logs}"
    assert any("all 2 router(s) dropped" in l for l in logs), f"{chart_name} fallback: {logs}"
    print(f"ok    {chart_name}: fallback on 401 + @rank DP suffix")

    # 3. Router-2-in-preStop race: R2 temporarily disappears from Ready router DNS
    #    after R1 restarted clean -> pod must keep waiting until R2 is back Ready.
    st = MockState(chart_name, my_ip, eport, rport)
    st.remove_noop = True
    st.dns = {ready_fqdn: [peer_ip], router_fqdn: ["10.99.0.20"]}  # only 1 of 2 routers Ready
    clean_workers = [w for w in base_workers(my_url, peer_url, decode_url) if w["id"] != "id-p2"]
    st.routers = {"10.99.0.20": list(clean_workers), "10.99.0.21": list(base_workers(my_url, peer_url, decode_url))}

    def tick_race(s: MockState, sc: int):
        if sc == 4:
            # R2 finishes restart & returns Ready with clean 1P1D
            s.routers["10.99.0.21"] = list(clean_workers)
            s.dns[router_fqdn] = ["10.99.0.20", "10.99.0.21"]

    st.tick_cb = tick_race
    logs = run_prestop(script, st)
    assert st.sleep_count >= 4, f"{chart_name} race: exited while only 1 router Ready! logs={logs}"
    assert any("all 2 router(s) dropped" in l for l in logs), f"{chart_name} race: {logs}"
    print(f"ok    {chart_name}: waits while peer router is mid-preStop (len<2)")

    # 4. Atomic rollout / 1->0 escape hatch: no surviving Ready peers -> skips router wait.
    st = MockState(chart_name, my_ip, eport, rport)
    st.remove_status = 500
    st.dns = {ready_fqdn: [], router_fqdn: ["10.99.0.20", "10.99.0.21"]}
    st.routers = {
        "10.99.0.20": base_workers(my_url, peer_url, decode_url),
        "10.99.0.21": base_workers(my_url, peer_url, decode_url),
    }
    logs = run_prestop(script, st)
    assert any("no surviving Ready peers" in l for l in logs), f"{chart_name} 1->0: {logs}"
    assert st.sleep_count <= 3, f"{chart_name} 1->0: waited too long ({st.sleep_count}); logs={logs}"
    print(f"ok    {chart_name}: atomic rollout / 1->0 skips router wait")

    # 5. Last-worker escape hatch: router lists only this pod for role -> never calls remove, skips immediately.
    st = MockState(chart_name, my_ip, eport, rport)
    st.dns = {ready_fqdn: [peer_ip], router_fqdn: ["10.99.0.20", "10.99.0.21"]}
    only_me = [
        {"id": "id-p2", "url": my_url, "worker_type": "prefill"},
        {"id": "id-d1", "url": decode_url, "worker_type": "decode"},
    ]
    st.routers = {"10.99.0.20": list(only_me), "10.99.0.21": list(only_me)}
    logs = run_prestop(script, st)
    assert len(st.remove_calls) == 0, f"{chart_name} last-worker sent remove! {st.remove_calls}; logs={logs}"
    assert any("last worker of role on router" in l for l in logs), f"{chart_name} last-worker: {logs}"
    print(f"ok    {chart_name}: last worker of role skips remove & wait")


def main() -> int:
    repo = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    for name in ("sglang", "vllm"):
        run_suite_for_chart(name, os.path.join(repo, "charts", name))
    print("ALL PD PRESTOP TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
