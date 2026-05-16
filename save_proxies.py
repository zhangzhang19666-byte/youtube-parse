"""拉取 SOCKS5 代理, 验证可用性, 保存到文件."""

import requests
import hashlib
import time
import concurrent.futures
import threading
from datetime import datetime

API_URL = "https://service.iiilab.com/api/web/extract"
SECRET_KEY = "JSnHKQfP1IlzIQzs"
TEST_URL = "https://www.youtube.com/watch?v=ou3MCs79Lm0"
PROXY_SOURCE = "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=socks5&timeout=5000&country=all"
OUTPUT_FILE = "working_proxies.txt"


def fetch_socks5():
    print("[1/3] 拉取 SOCKS5 列表 ...", end=" ", flush=True)
    try:
        r = requests.get(PROXY_SOURCE, timeout=15)
        proxies = [f"socks5://{l.strip()}" for l in r.text.splitlines()
                   if l.strip() and ":" in l.strip()]
        print(f"{len(proxies)} 个")
        return proxies
    except Exception as e:
        print(f"失败: {e}")
        return []


def validate_batch(candidates, target=30, workers=40):
    print(f"[2/3] 并发验证 {len(candidates)} 个 (目标 {target} 个可用) ...")
    valid = []
    lock = threading.Lock()
    tested = [0]
    t0 = time.perf_counter()

    def test_one(proxy_str):
        ts = str(int(time.time()))
        sig = hashlib.md5((TEST_URL + "youtube" + ts + SECRET_KEY).encode()).hexdigest()
        try:
            r = requests.post(API_URL,
                json={"url": TEST_URL, "site": "youtube"},
                headers={
                    "Content-Type": "application/json",
                    "G-Timestamp": ts,
                    "G-Footer": sig,
                    "Origin": "https://youtube.iiilab.com",
                    "Referer": "https://youtube.iiilab.com/",
                },
                proxies={"http": proxy_str, "https": proxy_str},
                timeout=10,
            )
            with lock:
                tested[0] += 1
            if r.ok:
                return (proxy_str, "OK", r.elapsed.total_seconds())
            else:
                msg = r.json().get("message", "")
                if "频繁" in msg or "明天" in msg:
                    return (proxy_str, "LIMITED", r.elapsed.total_seconds())
                return (None, f"HTTP{r.status_code}", 0)
        except Exception as e:
            with lock:
                tested[0] += 1
            return (None, str(e)[:50], 0)

    t0 = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(test_one, p) for p in candidates]
        for f in concurrent.futures.as_completed(futures):
            result = f.result()
            proxy, status, elapsed = result
            if proxy:
                valid.append((proxy, status, elapsed))
                if len(valid) >= target:
                    ex.shutdown(wait=False, cancel_futures=True)
                    break
            if tested[0] % 50 == 0:
                dur = time.perf_counter() - t0
                print(f"  已验证 {tested[0]}/{len(candidates)}  有效 {len(valid)}  "
                      f"速率 {tested[0]/dur:.0f}/s")

    dur = time.perf_counter() - t0
    print(f"  完成: {len(valid)} 个可用 (耗时 {dur:.0f}s, {tested[0]/dur:.0f}/s)")

    fresh = [(p, s, t) for p, s, t in valid if s == "OK"]
    limited = [(p, s, t) for p, s, t in valid if s == "LIMITED"]
    print(f"  全新可用: {len(fresh)}  已被限流但连通: {len(limited)}")

    return fresh, limited


def save_proxies(fresh, limited):
    print(f"\n[3/3] 保存到 {OUTPUT_FILE} ...")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(f"# iiilab API 可用 SOCKS5 代理\n")
        f.write(f"# 更新时间: {now}\n")
        f.write(f"# 格式: socks5://host:port  |  状态  |  延迟\n")
        f.write(f"# {'='*50}\n")

        if fresh:
            f.write(f"\n# --- 全新可用 ({len(fresh)} 个) ---\n")
            for proxy, status, elapsed in fresh:
                f.write(f"{proxy}  |  {status}  |  {elapsed:.1f}s\n")
                print(f"  {proxy}  OK  {elapsed:.1f}s")

        if limited:
            f.write(f"\n# --- 已被限流但连通 ({len(limited)} 个) ---\n")
            for proxy, status, elapsed in limited:
                f.write(f"{proxy}  |  {status}  |  {elapsed:.1f}s\n")
                print(f"  {proxy}  LIMITED  {elapsed:.1f}s")

    print(f"\n  共保存 {len(fresh) + len(limited)} 个代理到 {OUTPUT_FILE}")
    print(f"  全新可用: {len(fresh)}  已被限流: {len(limited)}")


def main():
    print(f"{'='*55}")
    print(f"  SOCKS5 代理提取器")
    print(f"  时间: {datetime.now():%H:%M:%S}")
    print(f"{'='*55}")

    candidates = fetch_socks5()
    if not candidates:
        print("拉取失败, 退出")
        return

    fresh, limited = validate_batch(candidates[:300], target=30)
    save_proxies(fresh, limited)

    print(f"\n  下次使用: python stress_test.py --proxies {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
