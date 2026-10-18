#!/usr/bin/env python3
"""命令行跑测试集，结果存到 results/，网页「历史对比」里能直接打开。

用法：
  python3 bench.py sets/search-pollution.json                  # DeepSeek
  python3 bench.py sets/search-pollution.json --engine tavily  # 对照组，需 TAVILY_API_KEY
  python3 bench.py sets/search-pollution.json --engine brave   # 对照组，需 BRAVE_API_KEY
  python3 bench.py sets/search-pollution.json --loc zh=223.5.5.5,en=8.8.8.8  # 按语言指定来源 IP

只依赖 Python 标准库。--engine 目前只对 search 类型的测试集生效。
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from server import ROOT, call_deepseek, load_key, write_manifests

RUNS_DIR = ROOT / "results"


def http_json(url, data=None, headers=None):
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    except Exception as e:
        status, raw = 0, json.dumps({"error": {"message": str(e)}}).encode()
    ms = round((time.perf_counter() - t0) * 1000)
    try:
        body = json.loads(raw)
    except ValueError:
        body = {"error": {"message": raw[:300].decode("utf-8", "replace")}}
    return status, ms, body


# 对照组：把各家返回统一成 DeepSeek 的字段（title / url / snippet / summary / date）
def search_tavily(query, count):
    status, ms, body = http_json("https://api.tavily.com/search",
                                 {"query": query, "max_results": count, "search_depth": "basic"},
                                 {"Authorization": f"Bearer {os.environ['TAVILY_API_KEY']}"})
    results = [{"title": r.get("title"), "url": r.get("url"), "snippet": r.get("content", ""),
                "summary": r.get("content", ""), "date": r.get("published_date")} for r in body.get("results", [])]
    return status, ms, ({"results": results} if status == 200 else body)


def search_brave(query, count):
    url = "https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode({"q": query, "count": min(count, 20)})
    req = urllib.request.Request(url, headers={"Accept": "application/json",
                                               "X-Subscription-Token": os.environ["BRAVE_API_KEY"]})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            status, body = resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        status, body = e.code, {"error": {"message": e.read()[:300].decode("utf-8", "replace")}}
    ms = round((time.perf_counter() - t0) * 1000)
    results = [{"title": r.get("title"), "url": r.get("url"), "snippet": r.get("description", ""),
                "summary": r.get("description", ""), "date": r.get("page_age")}
               for r in body.get("web", {}).get("results", [])]
    return status, ms, ({"results": results} if status == 200 else body)


def run_item(engine, set_type, item, count, fetch_top, loc=None):
    if set_type == "fetch":
        status, ms, body, retries = call_deepseek("fetch", {"url": item["url"]})
        return {"item": item, "resp": {"status": status, "ms": ms, "retries": retries, "body": body}}
    if engine == "deepseek":
        payload = {"query": item["query"], "count": count}
        ip = (loc or {}).get(item.get("lang"), (loc or {}).get("*"))
        if ip:
            payload["localization"] = ip
        status, ms, body, retries = call_deepseek("search", payload)
    else:
        status, ms, body = {"tavily": search_tavily, "brave": search_brave}[engine](item["query"], count)
        retries = 0
    row = {"item": item, "resp": {"status": status, "ms": ms, "retries": retries, "body": body}}
    if set_type == "probe":
        domains = item.get("expect_domain", [])
        seen, urls = set(), []
        for r in body.get("results", []):
            u = r["url"].split("#")[0]
            h = urllib.parse.urlparse(u).hostname or ""
            h = h[4:] if h.startswith("www.") else h
            if any(h == d or h.endswith("." + d) for d in domains) and u not in seen:
                seen.add(u)
                urls.append(u)
        row["fetches"] = []
        for u in urls[:fetch_top]:
            fs, fms, fbody, _ = call_deepseek("fetch", {"url": u})
            row["fetches"].append({"url": u, "status": fs, "ms": fms, "body": fbody})
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("set", help="测试集 JSON 路径")
    ap.add_argument("--engine", default="deepseek", choices=["deepseek", "tavily", "brave"])
    ap.add_argument("--count", type=int, default=10, help="每次搜索返回条数")
    ap.add_argument("--conc", type=int, default=3, help="并发数（DeepSeek 限流 5 QPS）")
    ap.add_argument("--loc", default="", help="按语言指定 localization IP，如 zh=223.5.5.5,en=8.8.8.8；* 表示全部")
    args = ap.parse_args()
    loc = dict(kv.split("=", 1) for kv in args.loc.split(",") if "=" in kv)

    need = {"deepseek": "DEEPSEEK_API_KEY", "tavily": "TAVILY_API_KEY", "brave": "BRAVE_API_KEY"}[args.engine]
    if args.engine == "deepseek" and not load_key() or args.engine != "deepseek" and not os.environ.get(need):
        sys.exit(f"缺少 {need}")

    path = Path(args.set)
    s = json.loads(path.read_text())
    started = datetime.now(timezone.utc).astimezone()
    suffix = ("" if args.engine == "deepseek" else f"-{args.engine}") + ("-loc" if loc else "")
    run = {
        "id": started.strftime("%Y%m%d-%H%M%S-") + path.stem + suffix,
        "set_name": s["name"] + ("" if args.engine == "deepseek" else f"（{args.engine}）") + (f"（IP {args.loc}）" if loc else ""),
        "type": s["type"], "engine": args.engine, "started": started.isoformat(),
        "options": {"conc": args.conc, "count": args.count, "fetch_top": s.get("fetch_top", 3), "localization": loc or None},
    }
    done = 0

    def task(item):
        nonlocal done
        row = run_item(args.engine, s["type"], item, args.count, s.get("fetch_top", 3), loc)
        done += 1
        print(f"\r{done}/{len(s['items'])}", end="", flush=True, file=sys.stderr)
        return row

    with ThreadPoolExecutor(args.conc) as ex:
        run["rows"] = list(ex.map(task, s["items"]))
    print()
    RUNS_DIR.mkdir(exist_ok=True)
    out = RUNS_DIR / f"{run['id']}.json"
    out.write_text(json.dumps(run, ensure_ascii=False, indent=1))
    write_manifests()
    ok = sum(1 for r in run["rows"] if r["resp"]["status"] == 200)
    print(f"完成：{ok}/{len(run['rows'])} 成功 → {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
