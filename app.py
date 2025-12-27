import os
import re
import time
import math
import io
import secrets
import urllib.parse
import asyncio
from datetime import datetime, timezone, timedelta

import aiohttp
from quart import Quart, request, render_template, Response

import search_youtube

# ------------------------------------------------------------
# Quart setup
# ------------------------------------------------------------
app = Quart(__name__)
app.jinja_env.trim_blocks = True
app.jinja_env.lstrip_blocks = True

YT_BASE_URL = (os.environ.get("URL") or "https://www.googleapis.com/youtube/v3/").strip()
API_KEY = (os.environ.get("API_KEY") or "").strip()
FONT_PATH = (os.environ.get("FONT_PATH") or "").strip()  # 画像出力用フォント（日本語）
if YT_BASE_URL and not YT_BASE_URL.endswith("/"):
    YT_BASE_URL += "/"

JST = timezone(timedelta(hours=9))


# ------------------------------------------------------------
# Small in-memory caches
# ------------------------------------------------------------
SEARCH_CACHE: dict[tuple, tuple[float, list]] = {}
SEARCH_CACHE_TTL_SEC = 600  # 10 minutes

SHARE_CACHE: dict[str, tuple[float, list[dict]]] = {}
SHARE_TTL_SEC = 60 * 60  # 1 hour


def _cache_get(cache: dict, key, ttl: int):
    v = cache.get(key)
    if not v:
        return None
    ts, data = v
    if time.time() - ts > ttl:
        cache.pop(key, None)
        return None
    return data


def _cache_set(cache: dict, key, data):
    cache[key] = (time.time(), data)


def _share_cleanup():
    now = time.time()
    dead = [k for k, (ts, _v) in SHARE_CACHE.items() if now - ts > SHARE_TTL_SEC]
    for k in dead:
        SHARE_CACHE.pop(k, None)


def new_share_sid() -> str:
    _share_cleanup()
    return secrets.token_urlsafe(12)


# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------
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
    }


def iso_to_jst_str(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(JST)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return iso or ""


def extract_video_id(s: str) -> str | None:
    s = (s or "").strip()

    # raw ID
    if re.fullmatch(r"[0-9A-Za-z_-]{11}", s):
        return s

    try:
        p = urllib.parse.urlparse(s)
        host = (p.netloc or "").lower()
        path = (p.path or "").strip("/")

        # youtu.be/<id>
        if "youtu.be" in host and path:
            cand = path.split("/")[0]
            if re.fullmatch(r"[0-9A-Za-z_-]{11}", cand):
                return cand

        # watch?v=<id>
        qs = urllib.parse.parse_qs(p.query or "")
        if "v" in qs and qs["v"]:
            cand = qs["v"][0]
            if re.fullmatch(r"[0-9A-Za-z_-]{11}", cand):
                return cand

        # /shorts/<id>, /embed/<id>, /video/<id>
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


def build_share_items(sorce: list[dict], limit: int = 200) -> list[dict]:
    items: list[dict] = []
    for row in (sorce or [])[:max(1, int(limit))]:
        if not isinstance(row, dict):
            continue
        # your search_youtube output keys (best-effort)
        thumb = row.get("thumbnails") or row.get("thumbnail") or ""
        title = row.get("title") or ""
        url = row.get("video_url") or row.get("videoUrl") or ""
        ch = row.get("name") or ""
        items.append({"thumb": thumb, "title": title, "url": url, "channel": ch})
    return items


async def yt_get_json(session: aiohttp.ClientSession, endpoint: str, params: dict):
    url = YT_BASE_URL + endpoint + "?" + urllib.parse.urlencode(params)
    async with session.get(url) as resp:
        text = await resp.text()
        if resp.status != 200:
            raise RuntimeError(f"{endpoint} failed {resp.status}: {text}")
        try:
            js = await resp.json()
        except Exception:
            raise RuntimeError(f"{endpoint} invalid json: {text}")
        if "error" in js:
            raise RuntimeError(str(js["error"]))
        return js


# ------------------------------------------------------------
# Routes
# ------------------------------------------------------------
@app.get("/", strict_slashes=False)
async def home():
    return await render_template(
        "index.html",
        title="search_youtube",
        sorce=[],
        form=default_form(),
        share_sid="",
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

    cache_key = (
        word, from_date, to_date, channel_id,
        viewcount_min, viewcount_max, sub_min, sub_max,
        video_count, order
    )

    sorce = _cache_get(SEARCH_CACHE, cache_key, SEARCH_CACHE_TTL_SEC)
    if sorce is None:
        # Search adapter: try "newer" keyword-args signature, then fall back to old positional
        try:
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
        except TypeError:
            # Old signature (example): (channel_id, key_word, published_from, published_to, viewcount_level, subscribercount_level, video_count, is_get_comment)
            sorce = await search_youtube.search_youtube(
                channel_id, word, from_date, to_date, viewcount_min, sub_min, video_count, False
            )
        _cache_set(SEARCH_CACHE, cache_key, sorce)

    sid = new_share_sid()
    SHARE_CACHE[sid] = (time.time(), build_share_items(sorce, limit=500))

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
    }

    print("DEBUG share_sid:", sid, "results:", (len(sorce) if isinstance(sorce, list) else type(sorce)))
    return await render_template(
        "index.html",
        title="search_youtube",
        sorce=sorce if isinstance(sorce, list) else [],
        form=form,
        share_sid=sid,
    )


# ------------------------------------------------------------
# X share image
#   /share_image?sid=...&rows=6&cols=6&n=36&w=1600
# ------------------------------------------------------------
from PIL import Image, ImageDraw, ImageFont


def _load_font(size: int):
    candidates = []
    if FONT_PATH:
        candidates.append(FONT_PATH)
    # リポジトリ同梱（例: fonts/NotoSansJP-Regular.ttf）
    here = os.path.dirname(os.path.abspath(__file__))
    candidates += [
        os.path.join(here, "fonts", "NotoSansJP-Regular.ttf"),
        os.path.join(here, "fonts", "NotoSansCJK-Regular.ttc"),
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansJP-Regular.otf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for p in candidates:
        try:
            return ImageFont.truetype(p, size=size)
        except Exception:
            pass
    return ImageFont.load_default()


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font, max_width: int, max_lines: int = 2):
    text = (text or "").strip()
    if not text:
        return [""]
    lines = []
    cur = ""
    for ch in text:
        test = cur + ch
        w = draw.textlength(test, font=font)
        if w <= max_width or not cur:
            cur = test
        else:
            lines.append(cur)
            cur = ch
            if len(lines) >= max_lines:
                break
    if len(lines) < max_lines and cur:
        lines.append(cur)
    if len(lines) == max_lines:
        joined = "".join(lines)
        if len(joined) < len(text):
            last = lines[-1]
            while last and draw.textlength(last + "…", font=font) > max_width:
                last = last[:-1]
            lines[-1] = (last + "…") if last else "…"
    return lines


async def _fetch_image_bytes(session: aiohttp.ClientSession, url: str) -> bytes | None:
    if not url:
        return None
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as r:
            if r.status != 200:
                return None
            return await r.read()
    except Exception:
        return None


def _paste_thumb_contain(canvas: Image.Image, img: Image.Image, x: int, y: int, w: int, h: int):
    # ✅ no vertical crop: contain
    img = img.convert("RGB")
    img.thumbnail((w, h), Image.LANCZOS)
    ox = x + (w - img.width) // 2
    oy = y + (h - img.height) // 2
    canvas.paste(img, (ox, oy))


@app.get("/share_image", strict_slashes=False)
async def share_image():
    sid = (request.args.get("sid") or "").strip()
    if not sid or sid not in SHARE_CACHE:
        return Response("share cache not found (sid)", status=404)

    ts, items = SHARE_CACHE[sid]
    if time.time() - ts > SHARE_TTL_SEC:
        SHARE_CACHE.pop(sid, None)
        return Response("share cache expired", status=404)

    rows = max(1, int(request.args.get("rows", "3") or 3))
    cols = max(1, int(request.args.get("cols", "4") or 4))
    n = int(request.args.get("n", str(rows * cols)) or (rows * cols))
    n = max(1, min(n, len(items), rows * cols))
    out_w = int(request.args.get("w", "1600") or 1600)
    out_w = max(800, min(out_w, 3000))

    pad = 8  # 画像間の余白を少し詰める
    title_h = 60
    cell_w = (out_w - pad * (cols + 1)) // cols
    thumb_h = int(cell_w * 9 / 16)
    cell_h = thumb_h + title_h
    out_h = pad * (rows + 1) + cell_h * rows

    canvas = Image.new("RGB", (out_w, out_h), (255, 255, 255))
    draw = ImageDraw.Draw(canvas)
    font = _load_font(22)
    font_small = _load_font(18)

    target = items[:n]
    async with aiohttp.ClientSession() as session:
        blobs = await asyncio.gather(*[_fetch_image_bytes(session, it["thumb"]) for it in target])

    for i, it in enumerate(target):
        r = i // cols
        c = i % cols
        x0 = pad + c * (cell_w + pad)
        y0 = pad + r * (cell_h + pad)

        draw.rectangle([x0, y0, x0 + cell_w, y0 + thumb_h], outline=(230, 230, 230), width=1)

        b = blobs[i]
        if b:
            try:
                img = Image.open(io.BytesIO(b))
                _paste_thumb_contain(canvas, img, x0, y0, cell_w, thumb_h)
            except Exception:
                draw.rectangle([x0, y0, x0 + cell_w, y0 + thumb_h], fill=(245, 245, 245))
        else:
            draw.rectangle([x0, y0, x0 + cell_w, y0 + thumb_h], fill=(245, 245, 245))

        tx = x0
        ty = y0 + thumb_h + 6
        lines = _wrap_text(draw, it.get("title", ""), font, cell_w, max_lines=2)
        for li, line in enumerate(lines[:2]):
            draw.text((tx, ty + li * 26), line, fill=(20, 20, 20), font=font)

        ch = (it.get("channel") or "").strip()
        if ch:
            ch_line = _wrap_text(draw, ch, font_small, cell_w, max_lines=1)[0]
            draw.text((tx, y0 + thumb_h + title_h - 22), ch_line, fill=(120, 120, 120), font=font_small)

    buf = io.BytesIO()
    canvas.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return Response(buf.getvalue(), mimetype="image/png")
