# iiilab YouTube API 逆向与自动化 — 项目总结

> 日期: 2026-05-17  
> 目标: 逆向 `youtube.iiilab.com` 的视频解析 API，实现批量解析 + GitHub Actions 全自动运行

---

## 1. API 逆向

### 1.1 最终算法

```python
timestamp = str(int(time.time()))
signature = md5(url + "youtube" + timestamp + "JSnHKQfP1IlzIQzs")

headers = {
    "Content-Type": "application/json",
    "G-Timestamp": timestamp,
    "G-Footer": signature,
    "Origin": "https://youtube.iiilab.com",
    "Referer": "https://youtube.iiilab.com/",
}
body = {"url": url, "site": "youtube"}

# POST https://service.iiilab.com/api/web/extract
```

### 1.2 逆向路径

```
浏览器抓包 (1.txt)
  → 下载网站首页 → 识别 Next.js 应用
  → 定位 Extractor 组件 (chunk page-*.js, Module 1646)
  → 找到签名: h[l.W4] = j()(a + t + x + g)
  → 追踪 Module 524 (chunk 750)
  → 提取 eC = ["SlNuSEtRZlA=", "MUlseklRenM="]
  → base64 解码 → 密钥 = JSnHKQfP1IlzIQzs
  → MD5 验证通过 ← 与抓包值匹配
```

### 1.3 模块地图（密钥变更时 2 分钟定位）

| 模块 | Chunk | 内容 |
|------|-------|------|
| 2687 | 388 | Header 常量 (`G-Timestamp`, `G-Footer`) |
| 524 | 750 | API 配置 (`JR` URL, `eC` 密钥数组) |
| 1646 | page | Extractor 组件（签名逻辑） |
| 2970 | 388 | MD5 实现 |

---

## 2. 限流与突破

| 项目 | 值 |
|------|-----|
| 每 IP 配额 | **7-8 次** |
| 超限响应 | HTTP 400 `"您的操作太频繁，请稍作休息，明天再来！"` |
| 限流维度 | **IP 地址**（无 session/cookie/设备指纹） |
| 防护层 | Cloudflare |

### 方案演进

```
curl_cffi     → 无效（TLS 指纹无关）
Playwright    → 无效（浏览器环境无关）
HTTP 代理     → 无效（无法转发 HTTPS CONNECT）
SOCKS5 代理   → 有效 ← 最终方案
```

---

## 3. SOCKS5 代理池

### 3.1 有效代理源

| 源 | 候选数 | 类型 |
|----|--------|------|
| ProxyScrape API | ~1200 | 实时 API |
| TheSpeedX SOCKS-List | ~4600 | GitHub raw |
| monosans/proxy-list | ~650 | 每小时更新 |
| Skillter/ProxyGather | ~160 | 已验证列表 |
| **合计去重** | **~6600** | |

### 3.2 核心算法

```python
# 拉取 → 精筛（只保留延迟 <8s 的）→ 每代理最多用 5 次 → 挂死即标记移除
#            ↓
# 请求失败 → 自动重试（最多 2 次，换代理）
#            ↓
# 代理池不足 → 自动补充

class ProxyPool:
    next()       # 轮询取代理, 跳过 dead 和超额的
    mark_dead()  # 标记失效代理
    alive        # 存活代理数

def call_api(url, proxy, max_retries=2):
    for attempt in range(max_retries + 1):
        result = request(url, proxy)
        if result.ok: return result
        if proxy is dead: pool.mark_dead(proxy)  # 换代理重试
        proxy = pool.next()
```

### 3.3 最终参数

| 参数 | 值 | 说明 |
|------|-----|------|
| MAX_PER_PROXY | 5 | 低于 7 次限额，留安全余量 |
| 验证超时 | 12s | 只收 <8s 完成的快代理 |
| 验证并发 | 40 | 快速筛完 |
| 解析并发 | 4 | 低并发减少超时 |
| 代理池目标 | 15 | 少而精 |

---

## 4. 最终结果

### 4.1 GitHub Actions 自动运行

```
仓库: zhangzhang19666-byte/youtube-parse (公开)
触发: workflow_dispatch (手动) / schedule (每日 00:00)
耗时: 5 分 11 秒
结果: 70/70 (100%)  ← 全部成功
```

### 4.2 输出

每次运行自动提交到仓库 `output/` 目录：

| 文件 | 内容 |
|------|------|
| `youtube_urls_latest.txt` | 最新 360p 下载链接（纯 URL） |
| `youtube_detail_latest.txt` | 最新详情（标题/时长/大小/下载链接） |
| `youtube_urls_YYYYMMDD_HHMMSS.txt` | 带时间戳的历史记录 |
| `youtube_detail_YYYYMMDD_HHMMSS.txt` | 带时间戳的历史记录 |

### 4.3 版本迭代

| 版本 | 策略 | 结果 | 耗时 |
|------|------|------|------|
| v1 | 1 源, 20 代理, 无重试 | 46/70 (66%) | 3m46s |
| v2 | 9 源含死源, 20 代理 | 30/70 (43%) | 7m8s |
| v3 | 4 源, 30 代理 | 28/70 (40%) | 8m41s |
| **v4** | **4 源, 精筛<8s, 重试2次, 配额5** | **70/70 (100%)** | **5m11s** |

---

## 5. 仓库结构

```
youtube-parse/
├── .github/workflows/parse.yml     # GitHub Actions 工作流
├── src/parse_all.py                # 核心：代理池 + 批量解析 + 输出 txt
├── check.txt                       # YouTube 链接列表（70 条）
├── youtube_extract.py              # 单条解析（本地用）
├── stress_test.py                  # 本地压测工具
├── save_proxies.py                 # 代理提取器（本地用）
├── youtube_download.gs             # Google Apps Script 版
├── requirements.txt
├── output/                         # Actions 自动推送的结果
│   ├── youtube_urls_latest.txt
│   └── youtube_detail_latest.txt
└── PROJECT_SUMMARY.md
```

## 6. 使用方式

```bash
# GitHub Actions（推荐）
gh workflow run parse.yml                           # 手动触发
gh workflow run parse.yml -f proxy_target=20        # 自定义代理数

# 本地单条
python youtube_extract.py "https://www.youtube.com/watch?v=VIDEO_ID"

# 本地批量压测
python stress_test.py --proxy-target 15 --workers 4

# 提取可用代理
python save_proxies.py

# 拉取最新结果
git pull
```
