import os
import re
import time
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
if YT_BASE_URL and not YT_BASE_URL.endswith("/"):
    YT_BASE_URL += "/"

JST = timezone(timedelta(hours=9))


# ------------------------------------------------------------
# In-memory caches (Render free plan friendly)
# ------------------------------------------------------------
SEARCH_CACHE: dict[tuple, tuple[float, list]] = {}
SEARCH_CACHE_TTL_SEC = 600  # 10 minutes

# share_sid -> minimal list for image generation
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
        "video_type": "",
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


def _is_short_url(url: str) -> bool:
    u = (url or "").lower()
    return "/shorts/" in u


def _is_short_row(row: dict) -> bool:
    """Shorts判定は「/shorts/ URLになるか」で行う（推奨）。
    - YouTube Shortsは“縦/正方形 + 最大3分”に拡張されており、長さだけでは判定できません。
    - YouTube Data API v3 には Shorts フラグが無いので、search_youtube側で /shorts/{id} の挙動を見て
      video_url を /shorts/ か /watch で返す想定です。
    """
    url = (row.get("video_url") or row.get("videoUrl") or "").strip()
    return _is_short_url(url)


def _filter_video_type(rows: list[dict], video_type: str) -> list[dict]:
    vt = (video_type or "").strip().lower()
    if vt in ("short", "shorts"):
        return [r for r in (rows or []) if isinstance(r, dict) and _is_short_row(r)]
    if vt in ("normal", "video"):
        return [r for r in (rows or []) if isinstance(r, dict) and not _is_short_row(r)]
    return rows or []



def build_share_items(sorce: list[dict], limit: int = 500) -> list[dict]:
    """
    X用まとめ画像に必要な最低限だけ保持
    - thumb: サムネURL
    - title: 動画タイトル
    - channel: チャンネル名
    - url: 動画URL（/shorts/ か watch?v= かも含む）
    - is_short: bool
    """
    items: list[dict] = []
    for row in (sorce or [])[:max(1, int(limit))]:
        if not isinstance(row, dict):
            continue
        thumb = row.get("thumbnails") or row.get("thumbnail") or ""
        title = row.get("title") or ""
        url = row.get("video_url") or row.get("videoUrl") or ""
        ch = row.get("name") or ""
        items.append({"thumb": thumb, "title": title, "url": url, "channel": ch, "is_short": _is_short_row(row)})
    return items


def share_counts(items: list[dict]) -> dict:
    n_short = sum(1 for it in items if it.get("is_short"))
    n_norm = len(items) - n_short
    return {"total": len(items), "normal": n_norm, "short": n_short}


# ------------------------------------------------------------
# Fonts (Pillow)
# ------------------------------------------------------------
from PIL import Image, ImageDraw, ImageFont


def _font_supports_jp(font) -> bool:
    try:
        m = font.getmask("あ")
        return (m.size[0] * m.size[1]) > 0
    except Exception:
        return False


def _font_hint_message() -> str:
    return (
        "まとめ画像の日本語が□になる場合："
        "repo に fonts/NotoSansJP-VariableFont_wght.ttf 等を置いて、"
        "環境変数 FONT_PATH を fonts/NotoSansJP-VariableFont_wght.ttf（/区切り）または"
        " /opt/render/project/src/fonts/... に設定してください。"
    )


def _load_font(size: int):
    """
    Prefer env FONT_PATH -> repo fonts -> system fonts.
    """
    base_dir = os.path.dirname(__file__)
    local_candidates = [
        os.environ.get("FONT_PATH", "").strip(),
        os.path.join(base_dir, "fonts", "NotoSansJP-VariableFont_wght.ttf"),
        os.path.join(base_dir, "fonts", "NotoSansJP-Regular.otf"),
        os.path.join(base_dir, "fonts", "NotoSansJP-Regular.ttf"),
        os.path.join(base_dir, "fonts", "NotoSansCJKjp-Regular.otf"),
    ]
    sys_candidates = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansJP-Regular.otf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for p in (local_candidates + sys_candidates):
        if not p:
            continue
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
    # ✅ no crop: contain
    img = img.convert("RGB")
    img.thumbnail((w, h), Image.LANCZOS)
    ox = x + (w - img.width) // 2
    oy = y + (h - img.height) // 2
    canvas.paste(img, (ox, oy))


def _parse_bool(v: str | None, default: bool = True) -> bool:
    if v is None:
        return default
    s = str(v).strip().lower()
    return s in ("1", "true", "on", "yes", "y")


# ------------------------------------------------------------
# Routes: Search pages
# ------------------------------------------------------------
@app.get("/", strict_slashes=False)
async def home():
    return await render_template(
        "index.html",
        title="search_youtube",
        sorce=[],
        form=default_form(),
        share_sid="",
        share_counts={"total": 0, "normal": 0, "short": 0},
        font_hint=_font_hint_message(),
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
    video_type = request.args.get("video-type", "")  # "" = 両方

    cache_key = (
        word, from_date, to_date, channel_id,
        viewcount_min, viewcount_max, sub_min, sub_max,
        video_count, order
    )

    sorce = _cache_get(SEARCH_CACHE, cache_key, SEARCH_CACHE_TTL_SEC)
    if sorce is None:
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
            sorce = await search_youtube.search_youtube(
                channel_id, word, from_date, to_date, viewcount_min, sub_min, video_count, False
            )
        _cache_set(SEARCH_CACHE, cache_key, sorce)

    sorce_list = sorce if isinstance(sorce, list) else []
    sorce_list = _filter_video_type(sorce_list, video_type)

    # share sid for X-image
    sid = new_share_sid()
    items = build_share_items(sorce_list, limit=500)
    SHARE_CACHE[sid] = (time.time(), items)

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
        "video_type": video_type,
    }

    return await render_template(
        "index.html",
        title="search_youtube",
        sorce=sorce_list,
        form=form,
        share_sid=sid,
        share_counts=share_counts(items),
        font_hint=_font_hint_message(),
    )


# ------------------------------------------------------------
# X share image
#   mixed:
#     /share_image?sid=...&rows=6&cols=6&n=36&w=1600&show_title=1&show_channel=0
#   separate:
#     /share_image?sid=...&separate=1
#       &rows_norm=4&cols_norm=4&n_norm=16
#       &rows_short=3&cols_short=2&n_short=6
#       &w=1600&show_title=1&show_channel=0
# ------------------------------------------------------------
@app.get("/share_image", strict_slashes=False)
async def share_image():
    sid = (request.args.get("sid") or "").strip()
    if not sid or sid not in SHARE_CACHE:
        return Response("share cache not found (sid)", status=404)

    ts, items = SHARE_CACHE[sid]
    if time.time() - ts > SHARE_TTL_SEC:
        SHARE_CACHE.pop(sid, None)
        return Response("share cache expired", status=404)

    separate = _parse_bool(request.args.get("separate"), default=False)
    show_title = _parse_bool(request.args.get("show_title"), default=True)
    show_channel = _parse_bool(request.args.get("show_channel"), default=True)

    out_w = int(request.args.get("w", "1600") or 1600)
    out_w = max(800, min(out_w, 3000))
    gap = request.args.get("gap", "")
    try:
        pad = int(gap) if gap != "" else 8
    except Exception:
        pad = 10
    pad = max(0, min(pad, 40))

    # dynamic text area
    title_lines = 2 if show_title else 0
    channel_lines = 1 if show_channel else 0
    line_h = 26
    text_pad_top = 6
    text_pad_bottom = 6
    text_h = (title_lines + channel_lines) * line_h + (text_pad_top + text_pad_bottom if (title_lines + channel_lines) > 0 else 0)

    font = _load_font(22)
    font_small = _load_font(18)
    font_ok = _font_supports_jp(font)

    async def render_section(section_items: list[dict], rows: int, cols: int, n: int, cell_ratio: float, heading: str | None):
        """
        cell_ratio = width/height of thumbnail area (16/9 for normal, 9/16 for shorts)
        Returns: (Image, height)
        """
        if not section_items or n <= 0:
            # minimal 1px image to simplify composition
            img = Image.new("RGB", (out_w, 1), (255, 255, 255))
            return img, 0

        rows = max(1, int(rows))
        cols = max(1, int(cols))
        n = max(1, min(int(n), len(section_items), rows * cols))

        # layout
        header_h = 44 if heading else 0
        cell_w = (out_w - pad * (cols + 1)) // cols
        thumb_h = int(cell_w / cell_ratio)  # height = width / (w/h)
        cell_h = thumb_h + text_h
        out_h = header_h + pad * (rows + 1) + cell_h * rows

        canvas = Image.new("RGB", (out_w, out_h), (255, 255, 255))
        draw = ImageDraw.Draw(canvas)

        if heading:
            draw.text((pad, 10), heading, fill=(20, 20, 20), font=font)

        target = section_items[:n]
        async with aiohttp.ClientSession() as session:
            blobs = await asyncio.gather(*[_fetch_image_bytes(session, it.get("thumb", "")) for it in target])

        for i, it in enumerate(target):
            r = i // cols
            c = i % cols
            x0 = pad + c * (cell_w + pad)
            y0 = header_h + pad + r * (cell_h + pad)

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

            # Text area
            if font_ok and (show_title or show_channel):
                tx = x0
                ty = y0 + thumb_h + text_pad_top
                if show_title:
                    lines = _wrap_text(draw, it.get("title", ""), font, cell_w, max_lines=2)
                    for li, line in enumerate(lines[:2]):
                        draw.text((tx, ty + li * line_h), line, fill=(20, 20, 20), font=font)
                    ty += title_lines * line_h
                if show_channel:
                    ch = (it.get("channel") or "").strip()
                    if ch:
                        ch_line = _wrap_text(draw, ch, font_small, cell_w, max_lines=1)[0]
                        draw.text((tx, y0 + thumb_h + text_pad_top + title_lines * line_h), ch_line, fill=(120, 120, 120), font=font_small)

        return canvas, out_h

    if not separate:
        rows = max(1, int(request.args.get("rows", "3") or 3))
        cols = max(1, int(request.args.get("cols", "4") or 4))
        n = int(request.args.get("n", str(rows * cols)) or (rows * cols))
        n = max(1, min(n, len(items), rows * cols))
        canvas, _h = await render_section(items, rows, cols, n, 16 / 9, None)
        buf = io.BytesIO()
        canvas.save(buf, format="PNG", optimize=True)
        buf.seek(0)
        return Response(buf.getvalue(), mimetype="image/png")

    # separate shorts / normal
    normal_items = [it for it in items if not it.get("is_short")]
    short_items = [it for it in items if it.get("is_short")]

    rows_norm = int(request.args.get("rows_norm", "3") or 3)
    cols_norm = int(request.args.get("cols_norm", "4") or 4)
    n_norm = int(request.args.get("n_norm", str(rows_norm * cols_norm)) or (rows_norm * cols_norm))
    n_norm = max(0, min(n_norm, len(normal_items), rows_norm * cols_norm))

    rows_short = int(request.args.get("rows_short", "3") or 3)
    cols_short = int(request.args.get("cols_short", "2") or 2)
    n_short = int(request.args.get("n_short", str(rows_short * cols_short)) or (rows_short * cols_short))
    n_short = max(0, min(n_short, len(short_items), rows_short * cols_short))

    # Render both sections, then compose vertically
    norm_img, norm_h = await render_section(
        normal_items, rows_norm, cols_norm, n_norm, 16 / 9,
        f"通常動画  ({n_norm}/{len(normal_items)})" if normal_items else "通常動画 (0)"
    )
    short_img, short_h = await render_section(
        short_items, rows_short, cols_short, n_short, 9 / 16,
        f"ショート  ({n_short}/{len(short_items)})" if short_items else "ショート (0)"
    )

    total_h = max(1, norm_h + (pad if (norm_h and short_h) else 0) + short_h)
    out = Image.new("RGB", (out_w, total_h), (255, 255, 255))
    y = 0
    if norm_h:
        out.paste(norm_img, (0, 0))
        y += norm_h
    if norm_h and short_h:
        # separator
        draw = ImageDraw.Draw(out)
        draw.line([(0, y + pad // 2), (out_w, y + pad // 2)], fill=(230, 230, 230), width=2)
        y += pad
    if short_h:
        out.paste(short_img, (0, y))

    buf = io.BytesIO()
    out.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return Response(buf.getvalue(), mimetype="image/png")


# ------------------------------------------------------------
# Comment page (unchanged here)
#   You likely already have /comment implemented elsewhere.
# ------------------------------------------------------------