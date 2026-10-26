"""SGLang 容器的 preStop:等在途作业排空,再自己驱动关闭。

为什么需要它:SGLang 收到 SIGTERM 后确实会走优雅关闭,但 diffusion 模式下
worker 卡在去噪循环里、5 秒内应答不了 scheduler 的排空请求
(launch_server.py 里 _SCHEDULER_SHUTDOWN_TIMEOUT_MS=5000,写死),于是退化成
terminate → kill,在途作业连同产物一起丢失。实测:34 秒就被杀,一条约 66 秒的
作业没跑完、共享盘上没有产物。

所以这里在**空闲时**才发 SIGTERM —— 那时 scheduler 能立刻应答,优雅关闭正常完成。

排空之后还要再赖一会儿(LINGER):Server 容器没有 preStop,删 Pod 时它立刻收 SIGTERM、
约 5 秒就退了,而它手上那条在途任务的**记账**要靠别的副本接管后轮询 backend_url 完成 ——
那个地址就是本 Pod 的 IP。Pod 一消失,已经算好的视频就取不到了(H3 走 HTTP 取内容,
不读共享盘)。所以 LINGER 必须 > 租约(leaseSeconds),让接管必然发生在 Pod 还活着的时候。

三个参数的关系:
    LINGER > leaseSeconds
    terminationGracePeriodSeconds > 最长单条任务时间 + LINGER
"""
import json
import os
import signal
import time
import urllib.request

PORT = int(os.environ.get("SGLANG_PORT", "30010"))
DRAIN_DEADLINE = int(os.environ.get("PRESTOP_DRAIN_DEADLINE", "600"))
LINGER = int(os.environ.get("PRESTOP_LINGER", "180"))
JOBS_URL = f"http://127.0.0.1:{PORT}/v1/videos"

# SGLang 自己的状态词。注意执行中的作业报的也是 queued(实测:并发为 1,
# 后到的作业一直是 queued 直到前一个完成),所以"非终态"才是在途。
TERMINAL = {"completed", "succeeded", "success", "failed", "error", "cancelled", "canceled"}

# 别让 localhost 请求走注入的代理:集群若给 Pod 注入 HTTP_PROXY,代理去连 Pod 自己的端口
# 是连不上的,而"读不到"在下面会被当成"没有服务可排"。
OPEN = urllib.request.build_opener(urllib.request.ProxyHandler({})).open


def log(message):
    try:
        with open("/proc/1/fd/1", "a") as out:
            out.write(f"[prestop] {message}\n")
    except OSError:
        pass


def inflight():
    """在途作业数;读不到返回 None(和 0 区分开)。"""
    try:
        with OPEN(JOBS_URL, timeout=5) as response:
            payload = json.load(response)
    except Exception:
        return None
    items = payload.get("data") if isinstance(payload, dict) else payload
    return sum(
        1 for job in (items or [])
        if isinstance(job, dict) and str(job.get("status", "")).lower() not in TERMINAL
    )


deadline = time.monotonic() + DRAIN_DEADLINE
unreadable = 0
while time.monotonic() < deadline:
    count = inflight()
    if count == 0:
        log("drained: nothing in flight")
        break
    if count is None:
        # 读不到当作"还在忙"继续等:一次抖动不该让我们提前掐断作业。
        # 但连续 ~30s 读不到是另一回事 —— 没有服务可排(端口没起来/进程已死),
        # 再耗完 deadline 只会白占 GPU。
        unreadable += 1
        if unreadable >= 15:
            log("jobs endpoint unreadable for ~30s -- no server to drain")
            break
    else:
        unreadable = 0
        log(f"waiting: {count} job(s) in flight")
    time.sleep(2)
else:
    log(f"drain deadline reached after {DRAIN_DEADLINE}s, jobs may still be in flight")

# 排空了也别立刻退:Pod IP 还要留给接管的副本来取结果(见文件头说明)。
if LINGER > 0:
    log(f"linger {LINGER}s so a sibling replica can adopt the in-flight task")
    time.sleep(LINGER)

# 自己驱动关闭,而不是等 kubelet 的 SIGTERM —— 此刻 SGLang 空闲,
# scheduler 能立刻应答排空请求,优雅关闭会正常完成。
log("SIGTERM -> PID 1")
os.kill(1, signal.SIGTERM)

# PID 1 退出会拆掉 PID namespace,本进程也随之消失;能走到这里说明它卡住了。
time.sleep(20)
log("still up after 20s -- killing PID 1's children to release its do_wait")
keep = (1, os.getpid(), os.getppid())
for entry in os.listdir("/proc"):
    if not entry.isdigit() or int(entry) in keep:
        continue
    try:
        os.kill(int(entry), signal.SIGKILL)
    except OSError:
        pass
