import requests
import hashlib
import time
import sys
import os
import concurrent.futures
import threading
from collections import defaultdict
from datetime import datetime

API_URL = "https://service.iiilab.com/api/web/extract"
SECRET_KEY = "JSnHKQfP1IlzIQzs"
SITE = "youtube"
TEST_URL = "https://www.youtube.com/watch?v=ou3MCs79Lm0"
HEADERS_TEMPLATE = {
    "Content-Type": "application/json",
    "Origin": "https://youtube.iiilab.com",
    "Referer": "https://youtube.iiilab.com/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}

PROXY_SOURCE = "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=socks5&timeout=5000&country=all"
MAX_PER_PROXY = 6
VALIDATE_WORKERS = 30
VALIDATE_TIMEOUT = 8


# ═══ Proxy Pool ═══════════════════════════════════════════════

class ProxyPool:
    def __init__(self):
        self._proxies = []       # list of proxy URLs
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
    def count(self):
        return len(self._proxies)

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
            # All exhausted, reset
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

def load_urls(path):
    urls = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                urls.append(line)
    return urls


def make_request(url, proxy=None, timeout=30):
    timestamp = str(int(time.time()))
    sig = hashlib.md5((url + SITE + timestamp + SECRET_KEY).encode()).hexdigest()
    headers = {**HEADERS_TEMPLATE, "G-Timestamp": timestamp, "G-Footer": sig}
    proxies = {"http": proxy, "https": proxy} if proxy else None

    t0 = time.perf_counter()
    try:
        resp = requests.post(API_URL, json={"url": url, "site": SITE},
                           headers=headers, proxies=proxies, timeout=timeout)
        lat = (time.perf_counter() - t0) * 1000
        if resp.ok:
            data = resp.json()
            n = len(data.get("medias", [{}])[0].get("formats", []))
            return (lat, True, None, f"{n}fmt | {data.get('text','')[:35]}")
        else:
            msg = resp.json().get("message", resp.text[:80])
            return (lat, False, f"HTTP{resp.status_code}: {msg}", None)
    except requests.exceptions.ProxyError:
        return ((time.perf_counter() - t0) * 1000, False, "PROXY_DEAD", None)
    except requests.exceptions.Timeout:
        return ((time.perf_counter() - t0) * 1000, False, "TIMEOUT", None)
    except Exception as e:
        return ((time.perf_counter() - t0) * 1000, False, str(e)[:60], None)


def print_bar(current, total, width=30):
    filled = current * width // total
    return "|" * filled + " " * (width - filled)


def print_summary(oks, errors, duration, pool=None):
    if oks:
        oks.sort()
        n = len(oks)
        print(f"  延迟: min={oks[0]:.0f}  avg={sum(oks)/n:.0f}  "
              f"p50={oks[n//2]:.0f}  p95={oks[int(n*.95)]:.0f}  max={oks[-1]:.0f} ms")
    total = len(oks) + sum(errors.values())
    ok = len(oks)
    fail = sum(errors.values())
    print(f"  成功: {ok}/{total} ({100*ok/total:.1f}%)" if total else "  成功: N/A")
    if errors:
        for err, cnt in sorted(errors.items(), key=lambda x: -x[1])[:4]:
            print(f"    [{cnt:3d}x] {err[:70]}")
    if duration > 0 and ok > 0:
        print(f"  吞吐: {ok/duration:.1f} req/s  耗时: {duration:.1f}s")
    if pool:
        print(f"  代理: {pool.alive}/{pool.count} 存活")


# ═══ Stage 0: Fetch + Validate Proxies ═══════════════════════

def fetch_proxies():
    """Fetch SOCKS5 proxy list from ProxyScrape."""
    print("\n[拉取] ProxyScrape SOCKS5 ...", end=" ", flush=True)
    try:
        r = requests.get(PROXY_SOURCE, timeout=15)
        proxies = [f"socks5://{l.strip()}" for l in r.text.splitlines()
                   if l.strip() and ":" in l.strip()]
        print(f"{len(proxies)} 个")
        return proxies
    except Exception as e:
        print(f"FAIL: {e}")
        return []


def validate_proxies(candidates, target_count=20):
    """Test candidates against iiilab API, return working ones."""
    print(f"[验证] {len(candidates)} 个候选 -> 目标 {target_count} 个可用 ...")
    valid = []
    lock = threading.Lock()
    tested = [0]

    def test(p):
        lat, ok, err, _ = make_request(TEST_URL, proxy=p, timeout=VALIDATE_TIMEOUT)
        with lock:
            tested[0] += 1
        if ok:
            return (True, p, lat)
        elif err and "频繁" in err:
            return (True, p, lat)  # proxy works, just rate-limited
        return (False, p, err)

    t0 = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=VALIDATE_WORKERS) as ex:
        futures = [ex.submit(test, p) for p in candidates]
        for f in concurrent.futures.as_completed(futures):
            ok, p, lat = f.result()
            if ok:
                valid.append(p)
                if len(valid) >= target_count:
                    ex.shutdown(wait=False, cancel_futures=True)
                    break
            if tested[0] % 30 == 0:
                print(f"  [{tested[0]:4d}/{len(candidates)}] {print_bar(tested[0], len(candidates))} "
                      f"有效:{len(valid)} 耗时:{time.perf_counter()-t0:.0f}s")

    print(f"  -> {len(valid)} 个可用 (耗时 {time.perf_counter()-t0:.0f}s)")
    return valid


def build_pool(pool, target=20):
    """Fetch and validate until we have enough proxies."""
    print(f"\n{'='*60}")
    print(f"  构建代理池 (目标: {target} 个)")
    print(f"{'='*60}")

    for attempt in range(3):
        if pool.alive >= target:
            break
        print(f"\n-- 第 {attempt+1} 轮 --")
        candidates = fetch_proxies()
        if not candidates:
            continue
        # Sample from candidates to avoid overloading
        sample = candidates[:200]
        valid = validate_proxies(sample, target - pool.alive)
        pool.add(valid)
        print(f"  池状态: {pool.alive} 存活 / {pool.count} 总计")

    if pool.alive >= target:
        print(f"\n  OK 代理池就绪: {pool.alive} 个")
    else:
        print(f"\n  WARNING 仅 {pool.alive} 个可用, 可能不够")


# ═══ Stage 1: Baseline (no proxy) ═══════════════════════════

def stage1_baseline(urls, count=5):
    print(f"\n{'='*60}")
    print(f"  Stage 1: 无代理基准 ({count}条)")
    print(f"{'='*60}")
    oks, errs = [], defaultdict(int)
    t0 = time.perf_counter()
    for url in urls[:count]:
        lat, ok, err, info = make_request(url)
        print(f"  [{'OK' if ok else 'FAIL'}] {lat:6.0f}ms {info or err}")
        if ok:
            oks.append(lat)
        else:
            errs[err] += 1
        time.sleep(1.2)
    print_summary(oks, errs, time.perf_counter() - t0)


# ═══ Stage 2: Proxy Rotation ════════════════════════════════

def stage2_rotation(urls, pool, workers=5, label=""):
    count = len(urls)
    pool.reset_usage()
    print(f"\n{'='*60}")
    print(f"  Stage 2: 代理轮换 ({count}条, {workers}并发, {pool.alive}代理) {label}")
    print(f"{'='*60}")

    oks, errs = [], defaultdict(int)
    lock = threading.Lock()
    done = [0]

    def work(url):
        proxy = pool.next()
        lat, ok, err, info = make_request(url, proxy=proxy, timeout=40)
        if err == "PROXY_DEAD" and proxy:
            pool.mark_dead(proxy)
        with lock:
            done[0] += 1
            bar = print_bar(done[0], count)
            tag = f"[{proxy.split('://')[1][:16]}]" if proxy else "[direct]"
            print(f"  [{done[0]:3d}/{count}] [{bar}] {lat:5.0f}ms {tag} "
                  f"{'OK' if ok else (err or '')[:30]}")
        return (lat, ok, err, info)

    t0 = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(work, url) for url in urls]
        for f in concurrent.futures.as_completed(futures):
            lat, ok, err, info = f.result()
            if ok:
                oks.append(lat)
            else:
                errs[err] += 1
    print_summary(oks, errs, time.perf_counter() - t0, pool)


# ═══ Stage 3: Speed Sweep ═══════════════════════════════════

def stage3_sweep(urls, pool):
    print(f"\n{'='*60}")
    print(f"  Stage 3: 并发度扫描 ({len(urls)}条, {pool.alive}代理)")
    print(f"{'='*60}")

    results = []
    for w in [3, 5, 8, 12, 16]:
        oks, errs = [], defaultdict(int)
        pool.reset_usage()
        lock = threading.Lock()
        done = [0]

        def work(url):
            proxy = pool.next()
            lat, ok, err, _ = make_request(url, proxy=proxy, timeout=40)
            if err == "PROXY_DEAD" and proxy:
                pool.mark_dead(proxy)
            with lock:
                done[0] += 1
            return (lat, ok, err)

        t0 = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=w) as ex:
            futures = [ex.submit(work, url) for url in urls]
            for f in concurrent.futures.as_completed(futures):
                lat, ok, err = f.result()
                if ok:
                    oks.append(lat)
                else:
                    errs[err] += 1
        dur = time.perf_counter() - t0

        ok, fail = len(oks), sum(errs.values())
        if oks:
            oks.sort()
            avg, p50 = sum(oks)/len(oks), oks[len(oks)//2]
            p95 = oks[int(len(oks)*.95)]
        else:
            avg = p50 = p95 = 0
        tp = ok/dur if dur > 0 else 0
        sr = 100*ok/(ok+fail) if (ok+fail) else 0
        print(f"  w={w:2d}  成功={ok:3d}/{len(urls)}  avg={avg:5.0f}ms  p95={p95:5.0f}ms  "
              f"吞吐={tp:.1f}/s  成功率={sr:.0f}%  耗时={dur:.0f}s  代理存活={pool.alive}")
        results.append({"w": w, "ok": ok, "fail": fail, "avg": avg, "p95": p95,
                       "tp": tp, "sr": sr, "dur": dur})

    print(f"\n  {'并发':<6} {'成功':<6} {'avg':<8} {'p95':<8} {'吞吐/s':<8} {'成功率':<8}")
    print(f"  {'-'*50}")
    for r in results:
        print(f"  {r['w']:<6} {r['ok']:<6} {r['avg']:<8.0f} {r['p95']:<8.0f} "
              f"{r['tp']:<8.1f} {r['sr']:<8.0f}%")


# ═══ Stage 4: Full Run ══════════════════════════════════════

def stage4_full(urls, pool, workers=5):
    print(f"\n{'='*60}")
    print(f"  Stage 4: 全量运行 ({len(urls)}条, {workers}并发)")
    print(f"{'='*60}")

    oks, errs = [], defaultdict(int)
    results = {}
    pool.reset_usage()
    lock = threading.Lock()
    done = [0]

    def work(url):
        proxy = pool.next()
        lat, ok, err, info = make_request(url, proxy=proxy, timeout=40)
        if err == "PROXY_DEAD" and proxy:
            pool.mark_dead(proxy)
        with lock:
            done[0] += 1
            bar = print_bar(done[0], len(urls))
            print(f"  [{done[0]:3d}/{len(urls)}] [{bar}] {lat:5.0f}ms "
                  f"{'OK ' + (info or '')[:35] if ok else (err or '')[:30]}")
        return (url, lat, ok, err, info)

    t0 = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(work, url) for url in urls]
        for f in concurrent.futures.as_completed(futures):
            url, lat, ok, err, info = f.result()
            if ok:
                oks.append(lat)
                results[url] = info
            else:
                errs[err] += 1
    dur = time.perf_counter() - t0
    print_summary(oks, errs, dur, pool)

    if results:
        print(f"\n  --- 成功解析 {len(results)} 条 ---")
        for url, info in list(results.items())[:20]:
            print(f"  {info}")


# ═══ Main ═══════════════════════════════════════════════════

def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--urls", default="check.txt")
    p.add_argument("--no-fetch", action="store_true")
    p.add_argument("--workers", type=int, default=6)
    p.add_argument("--proxy-target", type=int, default=20)
    args = p.parse_args()

    urls_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), args.urls)
    if not os.path.exists(urls_path):
        print(f"File not found: {urls_path}")
        sys.exit(1)
    urls = load_urls(urls_path)

    print(f"{'='*60}")
    print(f"  iiilab API Stress Test (SOCKS5 proxy)")
    print(f"  URLs: {len(urls)}  Time: {datetime.now():%H:%M:%S}")
    print(f"{'='*60}")

    pool = ProxyPool()

    # Stage 0: Build proxy pool
    if not args.no_fetch:
        build_pool(pool, target=args.proxy_target)
    else:
        print("\n[跳过] 代理拉取 (--no-fetch)")

    # Stage 1: Baseline
    stage1_baseline(urls, count=5)

    if pool.alive < 3:
        print(f"\n  代理不足 ({pool.alive} < 3), 跳过代理阶段")
        return

    # Stage 2: Rotation with increasing concurrency
    for w, cnt in [(3, 15), (5, 40), (args.workers, min(70, len(urls)))]:
        if cnt <= len(urls):
            stage2_rotation(urls[:cnt], pool, workers=w, label=f"前{cnt}条")

    # Stage 3: Speed sweep
    stage3_sweep(urls, pool)

    # Stage 4: Full run
    pool.reset_usage()
    stage4_full(urls, pool, workers=args.workers)

    print(f"\n{'='*60}")
    print(f"  Done @ {datetime.now():%H:%M:%S}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
