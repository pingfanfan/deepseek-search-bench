#!/usr/bin/env python3
"""DeepSeek Search / Fetch 测试台 —— 本地代理服务。

API key 只留在本机：页面请求本服务，本服务再带上 key 转发给 DeepSeek。
key 读取顺序：环境变量 DEEPSEEK_API_KEY → 同目录 .env 文件。

用法：python3 server.py [端口，默认 8790]
"""
import json
import os
import re
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


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def send_json(self, obj, status=200):
        raw = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/status":
            return self.send_json({"has_key": bool(API_KEY)})
        if path == "/api/domains":
            return self.send_json(json.loads((ROOT / "domains.json").read_text()))
        if path == "/api/sets":
            sets = []
            for f in sorted(SETS_DIR.glob("*.json")):
                d = json.loads(f.read_text())
                sets.append({**d, "id": f.stem})
            return self.send_json(sets)
        if path == "/api/runs":
            runs = []
            for f in sorted(RUNS_DIR.glob("*.json"), reverse=True):
                d = json.loads(f.read_text())
                runs.append({k: d.get(k) for k in ("id", "set_name", "type", "started", "summary", "options")})
            return self.send_json(runs)
        m = re.fullmatch(r"/api/runs/([\w-]+)", path)
        if m:
            f = RUNS_DIR / f"{m.group(1)}.json"
            if not f.exists():
                return self.send_json({"error": "not found"}, 404)
            return self.send_json(json.loads(f.read_text()))
        if path in ("/", "/index.html"):
            self.path = "/index.html"
        elif not path.startswith("/static/"):
            return self.send_json({"error": "not found"}, 404)
        return super().do_GET()

    def do_POST(self):
        path = self.path.split("?")[0]
        if path in ("/api/search", "/api/fetch"):
            if not API_KEY:
                return self.send_json({"status": 0, "ms": 0, "retries": 0,
                                       "body": {"error": {"message": "未找到 DEEPSEEK_API_KEY"}}})
            status, ms, body, retries = call_deepseek(path.rsplit("/", 1)[1], self.read_json())
            return self.send_json({"status": status, "ms": ms, "retries": retries, "body": body})
        if path == "/api/runs":
            run = self.read_json()
            if not re.fullmatch(r"[\w-]+", run.get("id", "")):
                return self.send_json({"error": "bad id"}, 400)
            RUNS_DIR.mkdir(exist_ok=True)
            (RUNS_DIR / f"{run['id']}.json").write_text(json.dumps(run, ensure_ascii=False, indent=1))
            return self.send_json({"ok": True})
        return self.send_json({"error": "not found"}, 404)

    def do_DELETE(self):
        m = re.fullmatch(r"/api/runs/([\w-]+)", self.path.split("?")[0])
        if m:
            (RUNS_DIR / f"{m.group(1)}.json").unlink(missing_ok=True)
            return self.send_json({"ok": True})
        return self.send_json({"error": "not found"}, 404)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8790
    print(f"DeepSeek 测试台：http://localhost:{port}   key {'已加载' if API_KEY else '未找到'}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
