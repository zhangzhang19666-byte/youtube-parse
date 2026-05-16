"""GitHub Actions 入口 — 批量解析 YouTube 视频链接，输出 txt 文件。

用法:
  python src/parse_all.py --urls check.txt --proxy-target 20
"""

import hashlib
import os
import sys
import time
import concurrent.futures
import threading
from collections import defaultdict
from datetime import datetime, timezone, timedelta

import requests

API_URL = "https://service.iiilab.com/api/web/extract"
SECRET_KEY = "JSnHKQfP1IlzIQzs"
SITE = "youtube"
TEST_URL = "https://www.youtube.com/watch?v=ou3MCs79Lm0"
MAX_PER_PROXY = 6

# 免费 SOCKS5 代理源（已验证有效，raw text, 每行 ip:port）
PROXY_SOURCES = [
    # ProxyScrape API — ~1200 SOCKS5
    "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=socks5&timeout=5000&country=all",
    # TheSpeedX — ~4600 SOCKS5 (最大)
    "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/socks5.txt",
    # monosans — ~650, 每小时更新
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt",
    # Skillter — ~160, 已验证 working only
    "https://raw.githubusercontent.com/Skillter/ProxyGather/refs/heads/master/proxies/working-proxies-socks5.txt",
]
TZ = timezone(timedelta(hours=8))  # Asia/Shanghai


# ═══ Proxy Pool ═══════════════════════════════════════════════

class ProxyPool:
    def __init__(self):
        self._proxies = []
        self._index = 0
        self._lock = threading.Lock()
        self._usage = defaultdict(int)
        self._dead = set()

    def add(self, proxies):
        with self._lock:
            for p in proxies:
                if p not in self._proxies and p not in self._dead:
                    self._proxies.append(p)

    @property
    def alive(self):
        return len([p for p in self._proxies if p not in self._dead])

    def next(self):
        if not self._proxies:
            return None
        with self._lock:
            for _ in range(len(self._proxies) * 2):
                proxy = self._proxies[self._index % len(self._proxies)]
                self._index += 1
                if proxy in self._dead:
                    continue
                if self._usage[proxy] < MAX_PER_PROXY:
                    self._usage[proxy] += 1
                    return proxy
            self._usage.clear()
            proxy = self._proxies[self._index % len(self._proxies)]
            self._index += 1
            self._usage[proxy] += 1
            return proxy

    def mark_dead(self, proxy):
        if proxy:
            with self._lock:
                self._dead.add(proxy)

    def reset_usage(self):
        with self._lock:
            self._usage.clear()


# ═══ Helpers ═════════════════════════════════════════════════

def fetch_proxies():
    """从多个免费源拉取 SOCKS5 代理列表。"""
    all_proxies = set()
    for src in PROXY_SOURCES:
        try:
            r = requests.get(src, timeout=15)
            count = 0
            for line in r.text.splitlines():
                line = line.strip()
                if line and ":" in line and not line.startswith("#"):
                    all_proxies.add(f"socks5://{line}")
                    count += 1
            short = src.split("/")[-2] if "/" in src else src
            print(f"  [源] {short}: +{count}")
        except Exception as e:
            print(f"  [源] {src[:50]}: FAIL ({e})")
    result = list(all_proxies)
    print(f"  [总计] {len(result)} 个候选 (去重后)")
    return result


def validate_proxies(candidates, target=20):
    """验证代理对 iiilab API 的可用性，返回可用的代理列表。"""
    valid = []
    lock = threading.Lock()
    tested = [0]

    def test(p):
        ts = str(int(time.time()))
        sig = hashlib.md5((TEST_URL + SITE + ts + SECRET_KEY).encode()).hexdigest()
        try:
            r = requests.post(API_URL,
                json={"url": TEST_URL, "site": SITE},
                headers={"Content-Type": "application/json", "G-Timestamp": ts, "G-Footer": sig,
                         "Origin": "https://youtube.iiilab.com", "Referer": "https://youtube.iiilab.com/"},
                proxies={"http": p, "https": p}, timeout=10)
            with lock:
                tested[0] += 1
            if r.ok:
                return (True, p)
            elif "频繁" in r.json().get("message", ""):
                return (True, p)
            return (False, p)
        except Exception:
            with lock:
                tested[0] += 1
            return (False, p)

    t0 = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=30) as ex:
        futures = [ex.submit(test, p) for p in candidates]
        for f in concurrent.futures.as_completed(futures):
            ok, p = f.result()
            if ok:
                valid.append(p)
                if len(valid) >= target:
                    ex.shutdown(wait=False, cancel_futures=True)
                    break
    return valid


def build_pool(pool, target=20):
    """拉取并验证代理，构建代理池。"""
    for attempt in range(3):
        if pool.alive >= target:
            break
        print(f"[代理] 第{attempt+1}轮拉取...")
        candidates = fetch_proxies()
        if not candidates:
            continue
        valid = validate_proxies(candidates[:200], target - pool.alive)
        pool.add(valid)
    print(f"[代理] 池就绪: {pool.alive} 个可用")


def call_api(url, proxy=None, timeout=30):
    """调用 iiilab API 解析单个视频。"""
    ts = str(int(time.time()))
    sig = hashlib.md5((url + SITE + ts + SECRET_KEY).encode()).hexdigest()
    proxies = {"http": proxy, "https": proxy} if proxy else None

    try:
        r = requests.post(API_URL,
            json={"url": url, "site": SITE},
            headers={"Content-Type": "application/json", "G-Timestamp": ts, "G-Footer": sig,
                     "Origin": "https://youtube.iiilab.com", "Referer": "https://youtube.iiilab.com/"},
            proxies=proxies, timeout=timeout)
        if r.ok:
            data = r.json()
            title = data.get("text", "?")
            fmt_360 = None
            for f in data.get("medias", [{}])[0].get("formats", []):
                if f.get("quality") == 360 and f.get("separate") == 0:
                    fmt_360 = f
                    break
            return {
                "ok": True,
                "title": title,
                "url_360p": fmt_360.get("video_url") if fmt_360 else None,
                "size_mb": round(fmt_360.get("video_size", 0) / 1048576, 1) if fmt_360 else 0,
                "duration": data.get("medias", [{}])[0].get("duration", 0),
            }
        else:
            msg = r.json().get("message", r.text[:80])
            return {"ok": False, "error": f"HTTP{r.status_code}: {msg}"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:80]}


# ═══ Main ════════════════════════════════════════════════════

def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--urls", default="check.txt")
    p.add_argument("--proxy-target", type=int, default=20)
    p.add_argument("--workers", type=int, default=5)
    args = p.parse_args()

    # Load URLs
    urls_path = args.urls
    if not os.path.isabs(urls_path):
        urls_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), urls_path)
    with open(urls_path, encoding="utf-8") as f:
        urls = [l.strip() for l in f if l.strip() and not l.startswith("#")]

    print(f"[开始] {len(urls)} 条 URL, 代理目标={args.proxy_target}")
    print(f"[时间] {datetime.now(TZ):%Y-%m-%d %H:%M:%S}")

    # Build proxy pool
    pool = ProxyPool()
    build_pool(pool, target=args.proxy_target)

    # Parse all URLs
    results = []
    oks = 0
    fails = 0
    lock = threading.Lock()
    done = [0]
    t0 = time.perf_counter()

    def worker(url):
        proxy = pool.next()
        info = call_api(url, proxy=proxy, timeout=40)
        if not info["ok"] and "PROXY" in str(info.get("error", "")):
            pool.mark_dead(proxy)
        with lock:
            done[0] += 1
            print(f"  [{done[0]:3d}/{len(urls)}] "
                  f"{'OK' if info['ok'] else 'FAIL'} "
                  f"{info.get('title', info.get('error', '?'))[:50]}")
        return info

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = [ex.submit(worker, url) for url in urls]
        for f in concurrent.futures.as_completed(futures):
            info = f.result()
            results.append(info)
            if info["ok"]:
                oks += 1
            else:
                fails += 1

    dur = time.perf_counter() - t0
    print(f"\n[完成] 成功={oks} 失败={fails} 耗时={dur:.0f}s")

    # Write output
    os.makedirs("output", exist_ok=True)
    now_str = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
    timestamp = datetime.now(TZ).strftime("%Y%m%d_%H%M%S")

    # youtube_urls.txt — 纯 URL
    url_lines = []
    detail_lines = []
    detail_lines.append(f"=== YouTube 视频解析结果 ===")
    detail_lines.append(f"解析时间: {now_str}")
    detail_lines.append(f"成功: {oks}  失败: {fails}  总计: {len(urls)}")
    detail_lines.append("=" * 80)

    for i, r in enumerate(results):
        detail_lines.append("")
        if r["ok"]:
            mins, secs = divmod(r["duration"], 60)
            detail_lines.append(f"[{i+1}] {r['title']}")
            detail_lines.append(f"    时长: {mins}分{secs}秒  |  360p: {r['size_mb']}MB")
            if r.get("url_360p"):
                detail_lines.append(f"    下载: {r['url_360p']}")
                url_lines.append(r["url_360p"])
            else:
                detail_lines.append(f"    下载: (无360p格式)")
        else:
            detail_lines.append(f"[{i+1}] 解析失败")
            detail_lines.append(f"    错误: {r.get('error', '?')}")

    detail_lines.append("")
    detail_lines.append("=" * 80)

    url_content = "\n".join(url_lines)
    detail_content = "\n".join(detail_lines)

    # 带时间戳的文件（保留历史）
    url_file_ts = f"output/youtube_urls_{timestamp}.txt"
    detail_file_ts = f"output/youtube_detail_{timestamp}.txt"

    # latest 文件（供 workflow commit）
    url_file_latest = "output/youtube_urls_latest.txt"
    detail_file_latest = "output/youtube_detail_latest.txt"

    for path, content in [
        (url_file_ts, url_content),
        (url_file_latest, url_content),
        (detail_file_ts, detail_content),
        (detail_file_latest, detail_content),
    ]:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

    print(f"[输出] {url_file_ts}  ({len(url_lines)} 条)")
    print(f"[输出] {detail_file_ts}  ({len(detail_lines)} 行)")
    print(f"[输出] {url_file_latest}")
    print(f"[输出] {detail_file_latest}")


if __name__ == "__main__":
    main()
