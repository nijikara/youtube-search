from quart import Quart, request, render_template
import search_youtube

app = Quart(__name__)

@app.get("/")
async def home():
    form = {
        "video_id": "",
        "word": "",
        "from": "",
        "to": "",
        "channel_id": "",
        "order": "date",
        "viewcount_min": "",
        "viewcount_max": "",
        "sub_min": "",
        "sub_max": "",
        "video_count": "1000",
        "comments": "",
    }
    return await render_template("index.html", title="search_youtube", sorce=[], is_get_comment=False, form=form)

@app.get("/scraping")
async def scraping():
    word = request.args.get("word", "")
    from_date = request.args.get("from", "")
    to_date = request.args.get("to", "")
    channel_id = request.args.get("channel-id", "")
    viewcount_min = request.args.get("viewcount-level", "")
    subscribercount_min = request.args.get("subscribercount-level", "")
    video_count = request.args.get("video-count", "1000")
    is_get_comment = (request.args.get("comments", "") == "true")

    # ★追加分
    order = request.args.get("order", "date")
    viewcount_max = request.args.get("viewcount-max", "")
    subscribercount_max = request.args.get("subscribercount-max", "")

    sorce = await search_youtube.search_youtube(
        channel_id, word, from_date, to_date,
        viewcount_min, subscribercount_min, video_count,
        is_get_comment,
        viewcount_max, subscribercount_max, order
    )

    form = {
        "video_id": request.args.get("video-id", ""),
        "word": word,
        "from": from_date,
        "to": to_date,
        "channel_id": channel_id,
        "order": order,
        "viewcount_min": viewcount_min,
        "viewcount_max": viewcount_max,
        "sub_min": subscribercount_min,
        "sub_max": subscribercount_max,
        "video_count": video_count,
        "comments": "true" if is_get_comment else "",
    }

    return await render_template("index.html", title="search_youtube", sorce=sorce, is_get_comment=is_get_comment, form=form)
