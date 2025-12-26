import os
import re
import io
import time
import math
import hashlib
import textwrap
import asyncio
import urllib.parse
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import aiohttp
from quart import Quart, request, render_template, Response

from PIL import Image, ImageDraw, ImageFont

import search_youtube

app = Quart(__name__)
app.jinja_env.trim_blocks = True
app.jinja_env.lstrip_blocks = True

YT_BASE_URL = (os.environ.get("URL") or "https://www.googleapis.com/youtube/v3/").strip()
API_KEY = (os.environ.get("API_KEY") or "").strip()
if YT_BASE_URL and not YT_BASE_URL.endswith("/"):
    YT_BASE_URL += "/"

JST = timezone(timedelta(hours=9))


# ---------------------------
# Quota (estimate)
# ---------------------------
class QuotaTracker:
    DAILY_LIMIT = int(os.environ.get("YT_QUOTA_DAILY_LIMIT") or "10000")

    def __init__(self):
        self.counts = {}  # method -> count

    def add(self, method: str, units: int = 1):
        self.counts[method] = self.counts.get(method, 0) + int(units)

    def used(self) -> int:
        return sum(self.counts.values())

    def remaining(self) -> int:
        return max(0, self.DAILY_LIMIT - self.used())

    def reset_at_jst_str(self) -> str:
        # YouTube Data API quota resets at midnight America/Los_Angeles
        try:
            la = ZoneInfo("America/Los_Angeles")
            now_la = datetime.now(la)
            tomorrow_midnight_la = datetime(
                now_la.year, now_la.month, now_la.day, 0, 0, 0, tzinfo=la
            ) + timedelta(days=1)
            reset_jst = tomorrow_midnight_la.astimezone(ZoneInfo("Asia/Tokyo"))
            return reset_jst.strftime("%Y-%m-%d %H:%M:%S JST")
        except Exception:
            return ""

    def snapshot_dict(self) -> dict:
        return {
            "used_estimate": self.used(),
            "remaining_estimate": self.remaining(),
            "reset_at_jst": self.reset_at_jst_str(),
            "note": "estimate (this process only)",
            "by_method": self.counts,
        }


quota = QuotaTracker()


# ---------------------------
# Common helpers
# ---------------------------
def _iso_to_jst_str(iso: str) -> str:
    try:
        dt = datetime.fromisoformat((iso or "").replace("Z", "+00:00")).astimezone(JST)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return iso or ""


def _iso_to_epoch(iso: str) -> int:
    try:
        dt = datetime.fromisoformat((iso or "").replace("Z", "+00:00")).astimezone(timezone.utc)
        return int(dt.timestamp())
    except Exception:
        return 0


def _trim_outer_blank_lines(s: str) -> str:
    # keep internal newlines; remove only leading/trailing blank lines
    s = (s or "").replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"^\s*\n+", "", s)
    s = re.sub(r"\n+\s*$", "", s)
    return s


def _user_id_from(author_channel_url: str, author_name: str) -> str:
    u = (author_channel_url or "").strip()
    try:
        if u:
            p = urllib.parse.urlparse(u)
            path = urllib.parse.unquote(p.path or "")

            m = re.search(r"/@([^/]+)", path)
            if m:
                return "@" + m.group(1)

            m2 = re.search(r"/channel/([^/]+)", path)
            if m2:
                return m2.group(1)
    except Exception:
        pass
    return (author_name or "").strip()


def extract_video_id(s: str) -> str | None:
    s = (s or "").strip()
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


async def yt_get_json(session: aiohttp.ClientSession, endpoint: str, params: dict, method: str):
    quota.add(method, 1)
    url = YT_BASE_URL + endpoint + "?" + urllib.parse.urlencode(params)
    async with session.get(url) as resp:
        text = await resp.text()
        if resp.status == 403 and "quotaExceeded" in text:
            raise RuntimeError(f"{endpoint} failed 403: {text}")
        if resp.status != 200:
            raise RuntimeError(f"{endpoint} failed {resp.status}: {text}")
        try:
            js = await resp.json()
        except Exception:
            raise RuntimeError(f"{endpoint} invalid json: {text}")
        if "error" in js:
            raise RuntimeError(str(js["error"]))
        return js


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


# ---------------------------
# Share image cache
# ---------------------------
SHARE_CACHE = {}
SHARE_TTL_SEC = 20 * 60  # 20min


def share_cache_set(sid: str, sorce: list, meta: dict):
    SHARE_CACHE[sid] = (time.time(), sorce, meta)


def share_cache_get(sid: str):
    v = SHARE_CACHE.get(sid)
    if not v:
        return None
    ts, sorce, meta = v
    if time.time() - ts > SHARE_TTL_SEC:
        SHARE_CACHE.pop(sid, None)
        return None
    return sorce, meta


def make_sid(params: dict) -> str:
    raw = repr(sorted(params.items())).encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:12]


def load_font(size: int) -> ImageFont.FreeTypeFont:
    # Put a Japanese font at: fonts/NotoSansJP-Regular.ttf
    font_path = os.path.join("fonts", "NotoSansJP-Regular.ttf")
    if os.path.exists(font_path):
        return ImageFont.truetype(font_path, size=size)
    return ImageFont.load_default()


def wrap_title(title: str, width_chars: int = 22, max_lines: int = 2) -> str:
    title = (title or "").strip()
    lines = textwrap.wrap(title, width=width_chars)
    if len(lines) <= max_lines:
        return "\n".join(lines)
    return "\n".join(lines[: max_lines - 1] + [lines[max_lines - 1] + "…"])


async def fetch_thumb(session: aiohttp.ClientSession, url: str):
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                return None
            b = await resp.read()
        im = Image.open(io.BytesIO(b)).convert("RGB")
        return im
    except Exception:
        return None


def resize_cover(im: Image.Image, w: int, h: int) -> Image.Image:
    iw, ih = im.size
    scale = max(w / iw, h / ih)
    nw, nh = int(iw * scale), int(ih * scale)
    im2 = im.resize((nw, nh))
    left = (nw - w) // 2
    top = (nh - h) // 2
    return im2.crop((left, top, left + w, top + h))


async def build_share_png(items: list, title_text: str, cols: int = 3, n: int = 12, width: int = 1600):
    cols = max(2, min(int(cols), 5))
    n = max(1, min(int(n), 50))
    items = items[:n]

    margin = 40
    gap = 24
    header_h = 120

    card_w = (width - margin * 2 - gap * (cols - 1)) // cols
    thumb_w = card_w
    thumb_h = int(card_w * 9 / 16)  # 16:9
    title_h = 92
    card_h = thumb_h + title_h + 18

    rows = math.ceil(len(items) / cols)
    height = header_h + margin + rows * card_h + (rows - 1) * gap + margin

    canvas = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(canvas)

    font_h1 = load_font(40)
    font_h2 = load_font(22)
    font_title = load_font(24)

    now = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
    draw.text((margin, 24), title_text, fill=(0, 0, 0), font=font_h1)
    draw.text((margin, 74), now, fill=(90, 90, 90), font=font_h2)

    async with aiohttp.ClientSession() as session:
        thumbs = await asyncio.gather(*[fetch_thumb(session, it.get("thumbnails", "")) for it in items])

    y0 = header_h + margin
    for idx, it in enumerate(items):
        r = idx // cols
        c = idx % cols
        x = margin + c * (card_w + gap)
        y = y0 + r * (card_h + gap)

        draw.rectangle([x, y, x + card_w, y + card_h], outline=(230, 230, 230), width=2)

        im = thumbs[idx]
        if im is None:
            draw.rectangle([x, y, x + thumb_w, y + thumb_h], fill=(245, 245, 245))
            draw.text((x + 12, y + 12), "NO IMAGE", fill=(120, 120, 120), font=font_h2)
        else:
            imc = resize_cover(im, thumb_w, thumb_h)
            canvas.paste(imc, (x, y))

        t = wrap_title(it.get("title", ""), width_chars=22, max_lines=2)
        draw.multiline_text((x + 10, y + thumb_h + 10), t, fill=(0, 0, 0), font=font_title, spacing=6)

    out = io.BytesIO()
    canvas.save(out, format="PNG")
    out.seek(0)
    return out.getvalue()


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
        quota=quota.snapshot_dict(),
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

    # call search_youtube with compatibility fallback
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
        # older signature fallback (positional)
        sorce = await search_youtube.search_youtube(
            channel_id, word, from_date, to_date,
            viewcount_min, sub_min, video_count,
            viewcount_max, sub_max, order
        )
    except Exception as e:
        sorce = [{"error": str(e)}]

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

    # share sid
    params_for_sid = {
        "word": word, "from": from_date, "to": to_date, "channel": channel_id,
        "vmin": viewcount_min, "vmax": viewcount_max,
        "smin": sub_min, "smax": sub_max,
        "count": video_count, "order": order,
    }
    sid = make_sid(params_for_sid)
    share_cache_set(sid, sorce, {"title": f"検索: {word or '(no keyword)'}"})

    return await render_template(
        "index.html",
        title="search_youtube",
        sorce=sorce,
        form=form,
        quota=quota.snapshot_dict(),
        share_sid=sid,
    )


@app.get("/comment", strict_slashes=False)
async def comment():
    # NOTE: comment.html is handled elsewhere; index links to /comment with video-id = URL
    raw = request.args.get("video-id", "")
    vid = extract_video_id(raw)
    if not vid:
        return Response("invalid video-id", status=400)
    return Response("comment.html is not included in this request. Keep your existing /comment implementation.", status=501)


@app.get("/share_image", strict_slashes=False)
async def share_image():
    sid = (request.args.get("sid", "") or "").strip()
    cols = request.args.get("cols", "3")
    n = request.args.get("n", "12")
    width = request.args.get("w", "1600")
    download = (request.args.get("download", "") or "").strip()

    cached = share_cache_get(sid)
    if not cached:
        return Response("share cache expired. search again.", status=404)

    sorce, meta = cached
    items = [x for x in (sorce or []) if isinstance(x, dict) and not x.get("error")]

    png = await build_share_png(
        items=items,
        title_text=meta.get("title", "YouTube Search"),
        cols=int(cols),
        n=int(n),
        width=int(width),
    )

    headers = {"Content-Type": "image/png"}
    if download:
        headers["Content-Disposition"] = f'attachment; filename="share_{sid}.png"'
    return Response(png, headers=headers)
