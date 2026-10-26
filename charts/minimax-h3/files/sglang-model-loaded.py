"""SGLang(diffusion)模型就绪/存活探针:同时要求 ①HTTP /health 200 且 ②模型驻留 GPU。

为什么两条都要:
- 只探 HTTP /health 不够:diffusion 模式下模型 worker 崩溃(如一次 CUDA OOM 打死 worker
  进程)后,FastAPI 主进程仍活、/health 照样返回 {"status":"ok"}(实测 /health、/liveness、
  /health_generate 三个端点都会假 ok),pod 被判 Ready、继续派活但没活 worker → 任务烂到
  24h 超时(实测僵尸潜伏 5 天)。模型死后 GPU 显存掉回 ~4MiB,故用 GPU 显存抓这个故障。
- 只探 GPU 也不够:加载期显存会先爬过阈值,但此时 HTTP 未必起好;加 /health 才能表达
  "服务真的能收请求了"。两条都过才算就绪/存活。

阈值:任一 GPU 显存 >= MODEL_LOADED_MIN_MIB(默认 10GiB)判"模型驻留";死亡态 ~4MiB,
tp4 常驻 ~48GB,取 10GiB 既能区分死亡又不误伤。nvidia-smi 缺失/超时时对 GPU 一项 fail-safe
放行(避免探针基建问题误重启),但 /health 仍是硬要求。
"""
import os
import subprocess
import sys
import urllib.request

MIN_MIB = int(os.environ.get("MODEL_LOADED_MIN_MIB", "10240"))
PORT = os.environ.get("SGLANG_PORT", "30010")

# ① HTTP /health 必须 200(服务进程起来、能收请求)
try:
    resp = urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=8)
    if resp.status != 200:
        print(f"/health status {resp.status}", file=sys.stderr)
        sys.exit(1)
except Exception as e:
    print(f"/health unreachable: {e}", file=sys.stderr)
    sys.exit(1)

# ② 模型驻留 GPU(抓"HTTP 活 / 模型死"的僵尸;/health 单独看不出来)
try:
    out = subprocess.check_output(
        ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
        timeout=8,
    ).decode()
    peak = max(int(x) for x in out.split() if x.strip().isdigit())
except Exception as e:  # nvidia-smi 缺失/超时:GPU 一项 fail-safe 放行,不误重启
    print(f"probe-skip GPU check: {e}", file=sys.stderr)
    sys.exit(0)
if peak >= MIN_MIB:
    sys.exit(0)
print(f"model not resident: peak GPU used {peak}MiB < {MIN_MIB}MiB", file=sys.stderr)
sys.exit(1)
