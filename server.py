#!/usr/bin/env python3
"""DeepSeek Search / Fetch 测试台：本地静态服务 + 命令行共用的请求函数。

网页是纯静态的，key 在网页里填，直接从浏览器发给 DeepSeek；
也可以不用这个脚本，直接打开 GitHub Pages 上的版本。
call_deepseek / load_key 给 bench.py 命令行用，key 读取顺序：环境变量 DEEPSEEK_API_KEY → 同目录 .env。

用法：python3 server.py [端口，默认 8790]
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SETS_DIR = ROOT / "sets"
RUNS_DIR = ROOT / "results"
API_BASE = "https://api.deepseek.com/v1/tools"


def load_key():
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    env_file = ROOT / ".env"
    if not key and env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.strip().startswith("DEEPSEEK_API_KEY="):
                key = line.split("=", 1)[1].strip().strip("'\"")
    return key


API_KEY = load_key()


def call_deepseek(tool, payload):
    """转发一次请求；429 时退避重试两次。返回 (http_status, 耗时ms, body, 重试次数)。"""
    data = json.dumps(payload).encode()
    retries = 0
    while True:
        req = urllib.request.Request(
            f"{API_BASE}/{tool}",
            data=data,
            headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
            method="POST",
        )
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                status, raw = resp.status, resp.read()
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read()
        except Exception as e:  # 网络错误、超时
            status, raw = 0, json.dumps({"error": {"message": str(e)}}).encode()
        ms = round((time.perf_counter() - t0) * 1000)
        if status == 429 and retries < 2:
            retries += 1
            time.sleep(1.5 * retries)
            continue
        try:
            body = json.loads(raw)
        except ValueError:
            body = {"error": {"message": raw[:500].decode("utf-8", "replace")}}
        return status, ms, body, retries


def write_manifests():
    """生成 sets/index.json 和 results/index.json，网页靠它们列出测试集和自带结果。"""
    (SETS_DIR / "index.json").write_text(json.dumps(
        sorted(f.stem for f in SETS_DIR.glob("*.json") if f.stem != "index"), ensure_ascii=False))
    runs = []
    for f in sorted(RUNS_DIR.glob("*.json"), reverse=True):
        if f.stem == "index":
            continue
        d = json.loads(f.read_text())
        runs.append({k: d.get(k) for k in ("id", "set_name", "type", "started", "summary", "options")})
    (RUNS_DIR / "index.json").write_text(json.dumps(runs, ensure_ascii=False))


class Handler(SimpleHTTPRequestHandler):
    """纯静态服务：网页本身不需要后端，这里只是给不想用 GitHub Pages 的人一个本地打开的办法。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self):
        # 本地工具，页面改了要立刻生效，不让浏览器缓存
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8790
    write_manifests()
    print(f"DeepSeek 测试台：http://localhost:{port}（在网页里填 API key）")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
