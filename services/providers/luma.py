# services/providers/luma.py
import os
import time
import asyncio
import logging
import aiohttp

log = logging.getLogger(__name__)

BASE = "https://api.lumalabs.ai/dream-machine/v1"
API_KEY = os.getenv("LUMA_API_KEY", "")

HEADERS_JSON = {
    "Authorization": f"Bearer {API_KEY}",
    "Accept": "application/json",
    "Content-Type": "application/json",
}
HEADERS_GET = {
    "Authorization": f"Bearer {API_KEY}",
    "Accept": "application/json",
}

def _raise_http(name: str, r: aiohttp.ClientResponse, body_text: str):
    # единый формат ошибки, чтобы в логах было видно статус и полный текст тела
    raise RuntimeError(f"{name} failed {r.status}. Body: {body_text}")

async def submit(prompt: str, aspect: str, speed: str, *, model: str = "ray-2"):
    """
    POST /dream-machine/v1/generations
    payload минимум: prompt, model, aspect_ratio
    """
    url = f"{BASE}/generations"
    payload = {
        "prompt": prompt,
        "model": model,            # ray-2 | ray-flash-2 | ray-1-6 (включено в аккаунте?)
        "aspect_ratio": aspect,    # "16:9" | "9:16"
        # опционально: "resolution": "720p", "duration": "5s"
    }
    async with aiohttp.ClientSession(trust_env=True) as s:
        async with s.post(url, headers=HEADERS_JSON, json=payload) as r:
            text = await r.text()
            if r.status >= 400:
                log.error("Luma POST %s failed %s\nBody: %s", url, r.status, text)
                _raise_http("Luma POST", r, text)
            try:
                data = await r.json()
            except Exception:
                log.error("Luma POST %s non-json response: %s", url, text)
                raise RuntimeError(f"Luma POST non-json response. Body: {text}")

    gen_id = data.get("id") or (data.get("generation") or {}).get("id")
    if not gen_id:
        log.error("Luma POST ok but no id in response: %s", data)
        raise RuntimeError(f"Luma POST ok but no id. Body: {data}")
    return {"job_id": gen_id, "raw": data}

async def poll(job_id: str):
    """
    GET /dream-machine/v1/generations/{id}
    ожидаем { state: ..., assets: { video: url } }
    """
    url = f"{BASE}/generations/{job_id}"
    async with aiohttp.ClientSession(trust_env=True) as s:
        async with s.get(url, headers=HEADERS_GET) as r:
            text = await r.text()
            if r.status >= 400:
                log.error("Luma GET %s failed %s\nBody: %s", url, r.status, text)
                _raise_http("Luma GET", r, text)
            try:
                data = await r.json()
            except Exception:
                log.error("Luma GET %s non-json response: %s", url, text)
                raise RuntimeError(f"Luma GET non-json response. Body: {text}")

    state = data.get("state")
    video_url = (data.get("assets") or {}).get("video")
    return {"status": state, "video_url": video_url, "raw": data}

async def wait_until_complete(job_id: str, *, interval_sec: int = 8, timeout_sec: int = 20 * 60):
    start = time.monotonic()
    last_state = None
    while True:
        info = await poll(job_id)
        state = info.get("status")
        if state != last_state:
            log.info("Luma job %s state -> %s", job_id, state)
            last_state = state

        if state in {"completed", "succeeded"} and info.get("video_url"):
            return {"final": "completed", "video_url": info["video_url"], "raw": info.get("raw")}
        if state in {"failed", "error"}:
            return {"final": "failed", "raw": info.get("raw")}

        if time.monotonic() - start > timeout_sec:
            return {"final": "timeout", "raw": info.get("raw")}

        await asyncio.sleep(interval_sec)

async def download_video(url: str) -> bytes:
    async with aiohttp.ClientSession(trust_env=True) as s:
        async with s.get(url) as r:
            text = await r.text() if r.status >= 400 else None
            if r.status >= 400:
                log.error("Luma video download failed %s\nBody: %s", r.status, text)
                _raise_http("Luma DOWNLOAD", r, text or "")
            return await r.read()