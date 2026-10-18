#!/usr/bin/env python3
"""信源构成分析：把一次 search 跑分结果按 domains.json 归类，输出 report/ 下的统计表。

用法：
  python3 analyze.py results/<主结果>.json [--compare results/<对照结果>.json]

--compare 用来对比两次运行（换 IP、换引擎）的结果重合度。只依赖标准库。
"""
import argparse
import collections
import json
import re
import statistics
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CFG = json.loads((ROOT / "domains.json").read_text())
AD_RE = re.compile(CFG["advertorial_title_re"], re.I)
# 软文只在「看起来中立」的类别里算：媒体、测评、社区、百科。厂商自己写榜单本来就是商业自荐
AD_CATS = {"新闻媒体", "测评/导购媒体", "社区/自媒体", "学术/百科"}
CAT_ORDER = ["官方/机构", "学术/百科", "新闻媒体", "测评/导购媒体", "社区/自媒体",
             "商业自荐/付费榜单", "软件仓库/镜像", "第三方下载站/山寨站", "垃圾/灰产", "其他"]


def host_path(url):
    u = urllib.parse.urlparse(url)
    h = (u.hostname or "").lower()
    return (h[4:] if h.startswith("www.") else h), u.path


def match(h, path, pat):
    d, _, p = pat.partition("/")
    return (h == d or h.endswith("." + d)) and (not p or path.startswith("/" + p))


def classify(url, item):
    h, path = host_path(url)
    if any(match(h, path, d) for d in item.get("expect_domain", [])):
        return "官方/机构"
    for c in CFG["categories"]:
        if any(match(h, path, d) for d in c["domains"]):
            return c["name"]
    return "其他"


def flatten(run):
    rows = []
    for r in run["rows"]:
        item = r["item"]
        for rank, x in enumerate(r["resp"]["body"].get("results", []), 1):
            cat = classify(x["url"], item)
            rows.append({
                "group": item["group"], "lang": item.get("lang", ""), "query": item["query"], "rank": rank,
                "url": x["url"], "host": host_path(x["url"])[0], "title": x.get("title") or "",
                "authority": x.get("authority"), "cat": cat,
                "ad": cat in AD_CATS and bool(AD_RE.search(x.get("title") or "")),
            })
    return rows


def pct(n, d):
    return f"{n / d * 100:.0f}%" if d else "–"


def table(headers, rows):
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    ap.add_argument("--compare")
    ap.add_argument("--out", default="report")
    ap.add_argument("--set", help="用这个测试集里的判定条件（expect_domain 等）覆盖结果文件里存的旧条件，按 query 对齐")
    args = ap.parse_args()
    run = json.loads(Path(args.run).read_text())
    if args.set:
        latest = {it["query"]: it for it in json.loads(Path(args.set).read_text())["items"]}
        for r in run["rows"]:
            r["item"] = latest.get(r["item"]["query"], r["item"])
    flat = flatten(run)
    groups = list(dict.fromkeys(r["group"] for r in flat))
    stats = {"run": run["id"], "set": run["set_name"], "n_queries": len(run["rows"]), "n_results": len(flat)}
    md = [f"# 信源构成分析\n\n运行：`{run['id']}`（{run['set_name']}），{len(run['rows'])} 个问题，{len(flat)} 条结果。\n"]

    # 1. 各组信源构成（全部 10 条 / 前 3 条）
    comp = {}
    for g in groups + ["全部"]:
        sub = [r for r in flat if g == "全部" or r["group"] == g]
        top3 = [r for r in sub if r["rank"] <= 3]
        c, c3 = collections.Counter(r["cat"] for r in sub), collections.Counter(r["cat"] for r in top3)
        comp[g] = {"n": len(sub), "all": {k: c[k] / len(sub) for k in CAT_ORDER},
                   "top3": {k: c3[k] / len(top3) for k in CAT_ORDER} if top3 else {}}
    stats["composition"] = comp
    md.append("## 1. 各组信源构成（前 10 条）\n")
    md.append(table(["分组", "条数"] + CAT_ORDER,
                    [[g, comp[g]["n"]] + [f"{comp[g]['all'][k] * 100:.0f}%" for k in CAT_ORDER] for g in groups + ["全部"]]))

    # 2. 权威度：API 自带的 authority 和人工类别是否一致
    auth = {}
    for k in CAT_ORDER:
        a = [r["authority"] for r in flat if r["cat"] == k and r["authority"] is not None and r["authority"] >= 0]
        if a:
            auth[k] = {"n": len(a), "mean": statistics.mean(a), "dist": {str(v): a.count(v) for v in range(5)}}
    stats["authority_by_cat"] = auth
    md.append("\n## 2. API 权威度（0–4）按人工类别\n")
    md.append(table(["类别", "条数", "平均", "0", "1", "2", "3", "4"],
                    [[k, v["n"], f"{v['mean']:.2f}"] + [v["dist"][str(i)] for i in range(5)] for k, v in auth.items()]))

    # 3. 软件下载：官网排位
    dl = [r for r in run["rows"] if r["item"].get("expect_domain") and r["item"]["group"] == "软件下载"]
    dl_stats = []
    for r in dl:
        res = r["resp"]["body"].get("results", [])
        cats = [classify(x["url"], r["item"]) for x in res]
        hosts = [host_path(x["url"])[0] for x in res]
        off = [i + 1 for i, x in enumerate(res)
               if any(match(*host_path(x["url"]), d) for d in r["item"]["expect_domain"])]
        dl_stats.append({"query": r["item"]["query"], "lang": r["item"].get("lang"), "first_official": off[0] if off else None,
                         "fake_in_top10": cats.count("第三方下载站/山寨站"), "top1": hosts[0] if hosts else ""})
    stats["downloads"] = dl_stats
    md.append("\n## 3. 软件下载：官网排第几\n")
    md.append(table(["问题", "语言", "官网首次出现", "第三方/山寨站数(前10)", "第 1 条"],
                    [[d["query"], d["lang"], d["first_official"] or "未出现", d["fake_in_top10"], d["top1"]] for d in dl_stats]))
    for lang in ("zh", "en"):
        sub = [d for d in dl_stats if d["lang"] == lang]
        if sub:
            stats[f"downloads_{lang}"] = {"n": len(sub), "official_top1": sum(d["first_official"] == 1 for d in sub),
                                          "official_top3": sum(bool(d["first_official"]) and d["first_official"] <= 3 for d in sub),
                                          "with_fake": sum(d["fake_in_top10"] > 0 for d in sub)}

    # 4. 疑似软文：中立类别里标题像「榜单 / 研究报告 / 品牌发布」的结果
    ads = [r for r in flat if r["ad"]]
    ad_by_group = {g: sum(r["ad"] for r in flat if r["group"] == g) / max(1, sum(r["group"] == g for r in flat)) for g in groups}
    stats["advertorial"] = {"n": len(ads), "by_group": ad_by_group,
                            "auth_mean": statistics.mean([r["authority"] for r in ads if r["authority"] is not None]) if ads else None,
                            "examples": [{k: r[k] for k in ("query", "rank", "host", "title", "authority")} for r in ads]}
    md.append(f"\n## 4. 疑似软文（{len(ads)} 条）\n\n规则：类别属于新闻媒体 / 测评 / 社区 / 百科，且标题命中 `{CFG['advertorial_title_re']}`。只是启发式，需人工复核。\n")
    md.append(table(["问题", "排名", "域名", "权威度", "标题"],
                    [[r["query"], r["rank"], r["host"], r["authority"], r["title"][:50]] for r in ads]))

    # 5. 低权威（≤1）结果都是什么
    low = [r for r in flat if r["authority"] is not None and 0 <= r["authority"] <= 1]
    stats["low_authority"] = {"n": len(low), "top3": sum(r["rank"] <= 3 for r in low),
                              "cats": dict(collections.Counter(r["cat"] for r in low))}
    md.append(f"\n## 5. 权威度 ≤1 的结果（{len(low)} 条，其中前 3 名 {stats['low_authority']['top3']} 条）\n")
    md.append(table(["问题", "排名", "域名", "类别", "标题"], [[r["query"], r["rank"], r["host"], r["cat"], r["title"][:40]] for r in low]))

    # 6. 两次运行对比（换 IP / 换引擎）
    if args.compare:
        other = json.loads(Path(args.compare).read_text())
        by_q = {r["item"]["query"]: r for r in other["rows"]}
        jac = collections.defaultdict(list)
        for r in run["rows"]:
            o = by_q.get(r["item"]["query"])
            if not o:
                continue
            a = {host_path(x["url"])[0] for x in r["resp"]["body"].get("results", [])}
            b = {host_path(x["url"])[0] for x in o["resp"]["body"].get("results", [])}
            j = len(a & b) / len(a | b) if a | b else 1
            jac[r["item"]["group"]].append(j)
            jac["全部"].append(j)
        stats["compare"] = {"other": other["id"], "jaccard": {g: statistics.mean(v) for g, v in jac.items()}}
        md.append(f"\n## 6. 与 `{other['id']}` 的域名重合度（Jaccard）\n")
        md.append(table(["分组", "平均重合度"], [[g, f"{v:.2f}"] for g, v in stats["compare"]["jaccard"].items()]))

    others = collections.Counter(r["host"] for r in flat if r["cat"] == "其他")
    stats["unclassified"] = dict(others.most_common())
    md.append(f"\n## 附：未归类域名（{len(others)} 个）\n\n" + ", ".join(f"{h}×{n}" for h, n in others.most_common()))

    out = ROOT / args.out
    out.mkdir(exist_ok=True)
    (out / "stats.json").write_text(json.dumps(stats, ensure_ascii=False, indent=1))
    (out / "report.md").write_text("\n".join(md) + "\n")
    (out / "results_flat.csv").write_text(
        "﻿group,lang,query,rank,host,category,authority,advertorial,title,url\n" + "\n".join(
            ",".join('"' + str(r[k]).replace('"', '""') + '"' for k in
                     ("group", "lang", "query", "rank", "host", "cat", "authority", "ad", "title", "url")) for r in flat) + "\n")
    print(f"→ {out.relative_to(ROOT)}/report.md, stats.json, results_flat.csv；未归类 {len(others)} 个域名")


if __name__ == "__main__":
    main()
