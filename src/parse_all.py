"""GitHub Actions 入口 — 批量解析 YouTube 视频链接，输出 txt 文件。

用法:
  python src/parse_all.py --urls check.txt --proxy-target 15
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
MAX_PER_PROXY = 5
TZ = timezone(timedelta(hours=8))  # Asia/Shanghai

PROXY_SOURCES = [
    "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=socks5&timeout=5000&country=all",
    "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/socks5.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt",
    "https://raw.githubusercontent.com/Skillter/ProxyGather/refs/heads/master/proxies/working-proxies-socks5.txt",
]


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
            # Try to find an under-capacity proxy
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
    """从多个免费源拉取 SOCKS5 代理。"""
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
    print(f"  [总计] {len(result)} 个候选 (去重)")
    return result


def validate_proxies(candidates, target=20):
    """快速验证代理对 iiilab API 的可用性 + 延迟。只保留 < 8s 的快代理。"""
    valid = []
    lock = threading.Lock()
    tested = [0]
    t0 = time.perf_counter()

    def test(p):
        t_start = time.perf_counter()
        ts = str(int(time.time()))
        sig = hashlib.md5((TEST_URL + SITE + ts + SECRET_KEY).encode()).hexdigest()
        try:
            r = requests.post(API_URL,
                json={"url": TEST_URL, "site": SITE},
                headers={"Content-Type": "application/json", "G-Timestamp": ts, "G-Footer": sig,
                         "Origin": "https://youtube.iiilab.com", "Referer": "https://youtube.iiilab.com/"},
                proxies={"http": p, "https": p}, timeout=12)
            elapsed = time.perf_counter() - t_start
            with lock:
                tested[0] += 1
            if r.ok and elapsed < 8.0:
                return (True, p, elapsed)
            elif "频繁" in r.json().get("message", "") and elapsed < 8.0:
                return (True, p, elapsed)  # proxy works, just rate-limited
            return (False, p, elapsed)
        except Exception:
            with lock:
                tested[0] += 1
            return (False, p, 99)

    t0 = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=40) as ex:
        futures = [ex.submit(test, p) for p in candidates]
        for f in concurrent.futures.as_completed(futures):
            ok, p, elapsed = f.result()
            if ok:
                valid.append(p)
                if len(valid) >= target:
                    ex.shutdown(wait=False, cancel_futures=True)
                    break
            if tested[0] % 100 == 0:
                dur = time.perf_counter() - t0
                print(f"  [验证] {tested[0]:4d}/{len(candidates)}  有效:{len(valid)}  "

                      f"{tested[0]/dur:.0f}/s")

    dur = time.perf_counter() - t0
    print(f"  [验证] 完成: {len(valid)} 个可用, 耗时 {dur:.0f}s ({tested[0]/dur:.0f}/s)")
    return valid


def build_pool(pool, target=20):
    """拉取并验证代理，构建高质量代理池。"""
    for attempt in range(2):
        if pool.alive >= target:
            break
        print(f"\n[代理] 第{attempt+1}轮拉取...")
        candidates = fetch_proxies()
        if not candidates:
            continue
        # 每轮最多验证 300 个，避免浪费时间
        sample = candidates[:300]
        valid = validate_proxies(sample, target - pool.alive)
        pool.add(valid)
        print(f"[代理] 池: {pool.alive} 存活")
    print(f"[代理] 最终池: {pool.alive} 个可用")


def call_api(url, proxy=None, timeout=25):
    """调用 iiilab API（短超时，失败快速重试）。"""
    ts = str(int(time.time()))
    sig = hashlib.md5((url + SITE + ts + SECRET_KEY).encode()).hexdigest()
    proxies = {"http": proxy, "https": proxy} if proxy else None

    t0 = time.perf_counter()
    try:
        r = requests.post(API_URL,
            json={"url": url, "site": SITE},
            headers={"Content-Type": "application/json", "G-Timestamp": ts, "G-Footer": sig,
                     "Origin": "https://youtube.iiilab.com", "Referer": "https://youtube.iiilab.com/"},
            proxies=proxies, timeout=timeout)
        lat = (time.perf_counter() - t0) * 1000
        if r.ok:
            data = r.json()
            title = data.get("text", "?")
            fmt_360 = None
            formats = data.get("medias", [{}])[0].get("formats", [])
            for f in formats:
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
            return {"ok": False, "error": f"HTTP{r.status_code}: {msg}", "proxy_dead": False}
    except (requests.exceptions.ProxyError, requests.exceptions.ConnectionError,
            requests.exceptions.Timeout):
        return {"ok": False, "error": "PROXY_FAIL", "proxy_dead": True}
    except Exception as e:
        return {"ok": False, "error": str(e)[:80], "proxy_dead": False}


# ═══ Main ════════════════════════════════════════════════════

def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--urls", default="check.txt")
    p.add_argument("--proxy-target", type=int, default=15)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--max-retries", type=int, default=2)
    args = p.parse_args()

    urls_path = args.urls
    if not os.path.isabs(urls_path):
        urls_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), urls_path)
    with open(urls_path, encoding="utf-8") as f:
        urls = [l.strip() for l in f if l.strip() and not l.startswith("#")]

    print(f"[开始] {len(urls)} 条 URL  代理目标={args.proxy_target}  workers={args.workers}")
    print(f"[时间] {datetime.now(TZ):%Y-%m-%d %H:%M:%S}")

    # Build proxy pool
    pool = ProxyPool()
    build_pool(pool, target=args.proxy_target)

    # Parse with retry
    results = [None] * len(urls)
    oks = 0
    fails = 0
    lock = threading.Lock()
    done = [0]
    pending = list(range(len(urls)))
    t0 = time.perf_counter()

    def process(idx):
        url = urls[idx]
        for attempt in range(args.max_retries + 1):
            proxy = pool.next()
            info = call_api(url, proxy=proxy, timeout=25)
            if info["ok"]:
                return info
            if info.get("proxy_dead"):
                pool.mark_dead(proxy)
            # Retry with a different proxy
            if attempt < args.max_retries and pool.alive > 0:
                time.sleep(0.3)
                continue
            break
        return info

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
        future_map = {ex.submit(process, i): i for i in range(len(urls))}
        for future in concurrent.futures.as_completed(future_map):
            idx = future_map[future]
            info = future.result()
            results[idx] = info
            with lock:
                done[0] += 1
                ok_flag = "OK" if info["ok"] else "FAIL"
                detail = info.get("title", info.get("error", "?"))[:45]
                print(f"  [{done[0]:3d}/{len(urls)}] {ok_flag} {detail}")
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

    url_lines = []
    detail_lines = [
        f"=== YouTube 视频解析结果 ===",
        f"解析时间: {now_str}",
        f"成功: {oks}  失败: {fails}  总计: {len(urls)}",
        "=" * 80,
    ]

    for i, r in enumerate(results):
        detail_lines.append("")
        if r["ok"]:
            m, s = divmod(r["duration"], 60)
            detail_lines.append(f"[{i+1}] {r['title']}")
            detail_lines.append(f"    时长: {m}分{s}秒  |  360p: {r['size_mb']}MB")
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

    for path, content in [
        (f"output/youtube_urls_{timestamp}.txt", url_content),
        ("output/youtube_urls_latest.txt", url_content),
        (f"output/youtube_detail_{timestamp}.txt", detail_content),
        ("output/youtube_detail_latest.txt", detail_content),
    ]:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

    print(f"[输出] {len(url_lines)} 条 URL")
    print(f"[输出] {len(detail_lines)} 行详情")


if __name__ == "__main__":
    main()
