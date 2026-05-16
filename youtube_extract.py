import requests
import hashlib
import time
import json
import sys

API_URL = "https://service.iiilab.com/api/web/extract"
SECRET_KEY = "JSnHKQfP1IlzIQzs"
SITE = "youtube"

HEADERS_TEMPLATE = {
    "Content-Type": "application/json",
    "Origin": "https://youtube.iiilab.com",
    "Referer": "https://youtube.iiilab.com/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
}


def extract_youtube(url: str) -> dict:
    """Extract video/audio/image links from a YouTube URL using iiilab API."""
    timestamp = str(int(time.time()))
    signature = hashlib.md5(
        (url + SITE + timestamp + SECRET_KEY).encode("utf-8")
    ).hexdigest()

    headers = {
        **HEADERS_TEMPLATE,
        "G-Timestamp": timestamp,
        "G-Footer": signature,
    }

    body = {"url": url, "site": SITE}

    resp = requests.post(API_URL, json=body, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()


def print_results(data: dict):
    """Pretty-print the extraction results."""
    print(f"\n{'='*60}")
    print(f"Title: {data.get('text', 'N/A')}")
    print(f"{'='*60}")

    for media in data.get("medias", []):
        media_type = media.get("media_type", "unknown")
        duration = media.get("duration", 0)
        preview = media.get("preview_url", "")

        print(f"\n--- {media_type.upper()} ({duration}s) ---")

        if preview:
            print(f"  Thumbnail: {preview}")

        if media_type == "video":
            print(f"  Default download: {media.get('resource_url', 'N/A')[:100]}...")

            for fmt in media.get("formats", []):
                quality = fmt.get("quality_note", "?")
                v_ext = fmt.get("video_ext", "?")
                v_size_mb = fmt.get("video_size", 0) / 1024 / 1024
                separate = " (含音频)" if fmt.get("separate") == 0 else " (视频/音频分离)"

                print(f"\n  [{quality}] .{v_ext} {v_size_mb:.1f}MB{separate}")
                print(f"    视频: {fmt.get('video_url', 'N/A')[:100]}...")

                if fmt.get("separate") == 1 and fmt.get("audio_url"):
                    a_ext = fmt.get("audio_ext", "?")
                    a_size_mb = fmt.get("audio_size", 0) / 1024 / 1024
                    print(f"    音频: {fmt.get('audio_url', 'N/A')[:100]}...")
                    print(f"           .{a_ext} {a_size_mb:.1f}MB")

        elif media_type == "audio":
            print(f"  Audio URL: {media.get('resource_url', 'N/A')[:100]}...")

        elif media_type == "image":
            print(f"  Image URL: {media.get('resource_url', 'N/A')}")

    if data.get("overseas") == 1:
        print(f"\n[注意] 下载海外平台资源需要网络环境支持")

    print(f"\n{'='*60}")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        url = sys.argv[1]
    else:
        url = input("请输入YouTube视频链接: ").strip()

    if not url:
        print("未输入链接，退出")
        sys.exit(1)

    print(f"正在解析: {url}")
    try:
        result = extract_youtube(url)
        print_results(result)
    except requests.exceptions.RequestException as e:
        print(f"网络请求失败: {e}")
        sys.exit(1)
    except json.JSONDecodeError:
        print("响应解析失败，请确认链接是否正确")
        sys.exit(1)
