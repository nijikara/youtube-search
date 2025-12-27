# app.py
import os
import re
import time
import math
import secrets
import urllib.parse
import io
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
from quart import Quart, request, render_template, Response

import search_youtube

# ---------------------------
# App
# ---------------------------
app = Quart(__name__)
app.jinja_env.trim_blocks = True
app.jinja_env.lstrip_blocks = True

# ---------------------------
# Config
# ---------------------------
YT_BASE_URL = (os.environ.get("URL") or "https://www.googleapis.com/youtube/v3/").strip()
API_KEY = (os.environ.get("API_KEY") or "").strip()
FONT_PATH = (os.environ.get("FONT_PATH") or "").strip()  # e.g. "fonts/NotoSansJP-VariableFont_wght.ttf"
if YT_BASE_URL and not YT_BASE_URL.endswith("/"):
    YT_BASE_URL += "/"

# ---------------------------
# Cache (search results)
# ---------------------------
CACHE: Dict[Tuple[Any, ...], Tuple[float, Any]] = {}
CACHE_TTL_SEC = 600  # 10 min

def cache_get(key: Tuple[Any, ...]):
    v = CACHE.get(key)
    if not v:
        return None
    ts, data = v
    if time.time() - ts > CACHE_TTL_SEC:
        CACHE.pop(key, None)
        return None
    return data

def cache_set(key: Tuple[Any, ...], data: Any):
    CACHE[key] = (time.time(), data)

# ---------------------------
# Share image cache (sid -> items)
# ---------------------------
SHARE_CACHE: Dict[str, Dict[str, Any]] = {}
SHARE_TTL_SEC = 3600  # 1 hour

def _now() -> float:
    return time.time()

def _clean_share_cache():
    now = _now()
    dead = [sid for sid, v in SHARE_CACHE.items() if now - float(v.get("ts", 0)) > SHARE_TTL_SEC]
    for sid in dead:
        SHARE_CACHE.pop(sid, None)

def new_share_sid() -> str:
    _clean_share_cache()
    return secrets.token_urlsafe(16)

def _is_shorts_url(url: str) -> bool:
    u = (url or "").lower()
    return ("youtube.com/shorts/" in u) or ("/shorts/" in u)

# ---------------------------
# Common helpers
# ---------------------------
def extract_video_id(s: str) -> Optional[str]:
    s = (s or "").strip()
    if re.fullmatch(r"[0-9A-Za-z_-]{11}", s):
        return s
    try:
        p = urllib.parse.urlparse(s)
        host = (p.netloc or "").lower()
        path = (p.path or "").strip("/")

        if "youtu.be" in host and path:
            cand = path.split("/")[0]
            if re.fullmatch(r"[0-9A-Za-z_-]{11}", cand):
                return cand

        qs = urllib.parse.parse_qs(p.query or "")
        if "v" in qs and qs["v"]:
            cand = qs["v"][0]
            if re.fullmatch(r"[0-9A-Za-z_-]{11}", cand):
                return cand

        parts = path.split("/")
        for i, token in enumerate(parts):
            if token in ("shorts", "embed", "video") and i + 1 < len(parts):
                cand = parts[i + 1]
                if re.fullmatch(r"[0-9A-Za-z_-]{11}", cand):
                    return cand
    except Exception:
        pass

    m = re.search(r"([0-9A-Za-z_-]{11})", s)
    return m.group(1) if m else None

def default_form():
    return {
        "word": "",
        "from": "",
        "to": "",
        "channel_id": "",
        "order": "date",
        "viewcount_min": "",
        "viewcount_max": "",
        "sub_min": "",
        "sub_max": "",
        "video_count": "200",
        "video_kind": "",  # ""=both, "normal", "shorts"
    }

# ---------------------------
# Build share items from current results
# ---------------------------
def build_share_items(sorce: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for row in (sorce or []):
        try:
            url = (row.get("video_url") or "").strip()
            thumb = (row.get("thumbnails") or "").strip()
            title = (row.get("title") or "").strip()
            channel = (row.get("name") or "").strip()
            if not thumb or not url:
                continue
            items.append({
                "thumb": thumb,
                "title": title,
                "channel": channel,
                "url": url,
                "is_shorts": _is_shorts_url(url),
            })
        except Exception:
            continue
    return items

# ---------------------------
# Routes
# ---------------------------
@app.get("/", strict_slashes=False)
async def home():
    return await render_template(
        "index.html",
        title="search_youtube",
        sorce=[],
        form=default_form(),
        share_sid="",
        share_counts={"all": 0, "normal": 0, "shorts": 0},
    )

@app.get("/scraping", strict_slashes=False)
async def scraping():
    word = request.args.get("word", "")
    from_date = request.args.get("from", "")
    to_date = request.args.get("to", "")
    channel_id = request.args.get("channel-id", "")

    viewcount_min = request.args.get("viewcount-level", "")
    viewcount_max = request.args.get("viewcount-max", "")
    sub_min = request.args.get("subscribercount-level", "")
    sub_max = request.args.get("subscribercount-max", "")
    video_count = request.args.get("video-count", "200")
    order = request.args.get("order", "date")

    video_kind = (request.args.get("video-kind", "") or "").strip().lower()

    cache_key = (
        word, from_date, to_date, channel_id,
        viewcount_min, viewcount_max, sub_min, sub_max,
        video_count, order
    )

    sorce = cache_get(cache_key)
    if sorce is None:
        sorce = await search_youtube.search_youtube(
            channel_id_input=channel_id,
            key_word=word,
            published_from=from_date,
            published_to=to_date,
            viewcount_min=viewcount_min,
            subscribercount_min=sub_min,
            video_count=video_count,
            viewcount_max=viewcount_max,
            subscribercount_max=sub_max,
            order=order,
        )
        cache_set(cache_key, sorce)

    # Filter by kind (post-filter; doesn't change quota)
    if isinstance(sorce, list) and video_kind in ("normal", "shorts"):
        if video_kind == "shorts":
            sorce = [r for r in sorce if _is_shorts_url((r.get("video_url") or ""))]
        else:
            sorce = [r for r in sorce if not _is_shorts_url((r.get("video_url") or ""))]

    # Share cache
    sid = new_share_sid()
    items = build_share_items(sorce if isinstance(sorce, list) else [])
    SHARE_CACHE[sid] = {
        "ts": _now(),
        "items": items,
        "counts": {
            "all": len(items),
            "normal": sum(1 for it in items if not it.get("is_shorts")),
            "shorts": sum(1 for it in items if it.get("is_shorts")),
        },
    }

    form = {
        "word": word,
        "from": from_date,
        "to": to_date,
        "channel_id": channel_id,
        "order": order,
        "viewcount_min": viewcount_min,
        "viewcount_max": viewcount_max,
        "sub_min": sub_min,
        "sub_max": sub_max,
        "video_count": video_count,
        "video_kind": video_kind,
    }

    return await render_template(
        "index.html",
        title="search_youtube",
        sorce=sorce if isinstance(sorce, list) else [],
        form=form,
        share_sid=sid,
        share_counts=SHARE_CACHE[sid]["counts"],
    )

# ---------------------------
# X-share image export
# ---------------------------
from PIL import Image, ImageDraw, ImageFont
import numpy as np

def _resolve_font_path(p: str) -> str:
    if not p:
        return ""
    p = p.strip().strip('"').strip("'")
    if not p:
        return ""
    # If relative, resolve from project root (same dir as this file)
    if not os.path.isabs(p):
        base = os.path.dirname(os.path.abspath(__file__))
        p = os.path.join(base, p)
    return p

def _load_font(size: int) -> ImageFont.FreeTypeFont:
    # Priority: FONT_PATH -> common JP fonts -> default
    candidates = []
    fp = _resolve_font_path(FONT_PATH)
    if fp:
        candidates.append(fp)

    # Render(Linux) typical font paths
    candidates += [
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansJP-Regular.otf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]

    for path in candidates:
        try:
            if path and os.path.exists(path):
                return ImageFont.truetype(path, size=size)
        except Exception:
            continue

    # Fallback
    return ImageFont.load_default()

def _wrap_by_pixels(text: str, font: ImageFont.FreeTypeFont, max_w: int, max_lines: int) -> List[str]:
    # Works for JP (no spaces). Keeps existing newlines as hard breaks.
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    parts = text.split("\n")
    lines: List[str] = []

    def _measure(s: str) -> int:
        # PIL >=10: getlength. fallback: getbbox.
        if hasattr(font, "getlength"):
            return int(font.getlength(s))
        box = font.getbbox(s)
        return int(box[2] - box[0])

    for part in parts:
        s = part
        if s == "":
            # keep empty line, but don't exceed max_lines too much
            if len(lines) < max_lines:
                lines.append("")
            continue

        buf = ""
        for ch in s:
            nxt = buf + ch
            if _measure(nxt) <= max_w:
                buf = nxt
                continue
            # line break
            lines.append(buf)
            buf = ch
            if len(lines) >= max_lines:
                break
        if len(lines) >= max_lines:
            break
        if buf:
            lines.append(buf)
        if len(lines) >= max_lines:
            break

    # truncate last line with ellipsis if still overflow
    if len(lines) > max_lines:
        lines = lines[:max_lines]

    if len(lines) == max_lines and (len(parts) > 1 or any(len(p) > 0 for p in parts)):
        # If original might have more content, add ellipsis if it doesn't fit.
        last = lines[-1]
        ell = "…"
        while last and _measure(last + ell) > max_w:
            last = last[:-1]
        lines[-1] = (last + ell) if last else ell

    return lines

def _trim_side_black_bars(img: Image.Image, threshold: int = 12, min_run_ratio: float = 0.07) -> Image.Image:
    """Trim solid-ish black columns on left/right (typical Shorts letterboxing)."""
    try:
        rgb = img.convert("RGB")
        arr = np.asarray(rgb)  # (h,w,3)
        h, w, _ = arr.shape
        if w < 40:
            return img
        col_mean = arr.mean(axis=(0, 2))  # (w,)
        min_run = max(10, int(w * min_run_ratio))

        left = 0
        while left < w and col_mean[left] <= threshold:
            left += 1
        right = 0
        while right < w and col_mean[w - 1 - right] <= threshold:
            right += 1

        # Only trim if it looks like real bars
        if left >= min_run and right >= min_run and (left + right) < w * 0.7:
            return img.crop((left, 0, w - right, h))
        return img
    except Exception:
        return img

def _paste_contain(canvas: Image.Image, img: Image.Image, x: int, y: int, w: int, h: int):
    if img.width <= 0 or img.height <= 0:
        return
    r = min(w / img.width, h / img.height)
    nw = max(1, int(img.width * r))
    nh = max(1, int(img.height * r))
    ix = x + (w - nw) // 2
    iy = y + (h - nh) // 2
    canvas.paste(img.resize((nw, nh), Image.LANCZOS), (ix, iy))

@app.get("/share_image", strict_slashes=False)
async def share_image():
    """
    Generate a single collage image for X.
    Params:
      sid: share cache id
      kind: normal | shorts | all
      n: number of items
      rows, cols: grid
      w: output width
      gap: spacing between cards
      show_title: 1/0
      show_channel: 1/0
    """
    sid = (request.args.get("sid", "") or "").strip()
    if not sid or sid not in SHARE_CACHE:
        return Response("invalid sid (search again and use the latest share buttons)", status=400)

    kind = (request.args.get("kind", "normal") or "normal").strip().lower()
    kind = kind if kind in ("normal", "shorts", "all") else "normal"

    show_title = (request.args.get("show_title", "1") or "1").strip() != "0"
    show_channel = (request.args.get("show_channel", "0") or "0").strip() != "0"

    try:
        out_w = int(request.args.get("w", "1200"))
    except Exception:
        out_w = 1200
    out_w = max(600, min(out_w, 6000))

    try:
        gap = int(request.args.get("gap", "8"))
    except Exception:
        gap = 8
    gap = max(0, min(gap, 50))

    items_all: List[Dict[str, Any]] = SHARE_CACHE[sid].get("items", []) or []
    if kind == "shorts":
        items = [it for it in items_all if it.get("is_shorts")]
    elif kind == "normal":
        items = [it for it in items_all if not it.get("is_shorts")]
    else:
        items = list(items_all)

    if not items:
        return Response("no items for the selected kind", status=400)

    try:
        n = int(request.args.get("n", str(len(items))))
    except Exception:
        n = len(items)
    n = max(1, min(n, len(items)))

    # rows/cols
    try:
        rows = int(request.args.get("rows", "0"))
    except Exception:
        rows = 0
    try:
        cols = int(request.args.get("cols", "0"))
    except Exception:
        cols = 0

    if rows <= 0 or cols <= 0:
        # near-square
        cols = int(math.ceil(math.sqrt(n)))
        rows = int(math.ceil(n / cols))
    rows = max(1, rows)
    cols = max(1, cols)

    # Ensure grid can hold n
    if rows * cols < n:
        cols = int(math.ceil(n / rows))

    # Layout
    pad = 14
    cell_w = int((out_w - pad * 2 - gap * (cols - 1)) / cols)
    cell_w = max(160, cell_w)

    # Thumb aspect: normal=16:9, shorts=9:16
    if kind == "shorts":
        thumb_h = int(cell_w * 16 / 9)  # vertical
    else:
        thumb_h = int(cell_w * 9 / 16)  # horizontal

    # Text area
    line_h = 26
    title_lines = 2 if show_title else 0
    channel_lines = 1 if show_channel else 0
    text_h = 0
    if show_title:
        text_h += title_lines * line_h
    if show_channel:
        text_h += channel_lines * line_h
    text_h += 16 if (show_title or show_channel) else 0  # padding

    cell_h = thumb_h + text_h
    out_h = pad * 2 + rows * cell_h + gap * (rows - 1)

    canvas = Image.new("RGB", (out_w, out_h), (255, 255, 255))
    draw = ImageDraw.Draw(canvas)

    # Fonts
    f_title = _load_font(22)
    f_channel = _load_font(18)

    # Fetch thumbs concurrently
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async def fetch_img(url: str) -> Optional[Image.Image]:
            if not url:
                return None
            try:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        return None
                    b = await resp.read()
                return Image.open(io.BytesIO(b)).convert("RGB")
            except Exception:
                return None

        thumbs = await asyncio_gather_limit([it["thumb"] for it in items[:n]], fetch_img, limit=10)


    # Draw cards
    for idx in range(n):
        r = idx // cols
        c = idx % cols
        x0 = pad + c * (cell_w + gap)
        y0 = pad + r * (cell_h + gap)

        # Card border
        draw.rectangle([x0, y0, x0 + cell_w, y0 + cell_h], outline=(200, 200, 200), width=2)

        # Thumb area border
        draw.rectangle([x0, y0, x0 + cell_w, y0 + thumb_h], outline=(230, 230, 230), width=1)

        img = thumbs[idx]
        if img is None:
            # placeholder
            draw.rectangle([x0, y0, x0 + cell_w, y0 + thumb_h], fill=(240, 240, 240))
        else:
            if kind == "shorts":
                img = _trim_side_black_bars(img)
            _paste_contain(canvas, img, x0, y0, cell_w, thumb_h)

        cur_y = y0 + thumb_h + 8

        # Title
        if show_title:
            t = (items[idx].get("title") or "").strip()
            t_lines = _wrap_by_pixels(t, f_title, max_w=cell_w - 14, max_lines=2)
            for ln in t_lines:
                draw.text((x0 + 7, cur_y), ln, font=f_title, fill=(0, 0, 0))
                cur_y += line_h

        # Channel
        if show_channel:
            ch = (items[idx].get("channel") or "").strip()
            ch_lines = _wrap_by_pixels(ch, f_channel, max_w=cell_w - 14, max_lines=1)
            cur_y += 4
            draw.text((x0 + 7, cur_y), ch_lines[0] if ch_lines else "", font=f_channel, fill=(80, 80, 80))

    # PNG response
    out = io.BytesIO()
    canvas.save(out, format="PNG")
    out.seek(0)
    return Response(out.read(), mimetype="image/png")

# ---------------------------
# Async gather with limit
# ---------------------------
import asyncio

async def asyncio_gather_limit(urls: List[str], func, limit: int = 10):
    sem = asyncio.Semaphore(limit)
    results: List[Optional[Image.Image]] = [None] * len(urls)

    async def _run(i: int, u: str):
        async with sem:
            results[i] = await func(u)

    await asyncio.gather(*[_run(i, u) for i, u in enumerate(urls)])
    return results
