from quart import Quart, render_template, request
import datetime
import get_comment_by_id
import search_youtube
import common

app = Quart(__name__, static_folder='./templates/images')

@app.route('/')
async def hello():
    return await render_template('layout.html', title='search_youtube')

# /scrapingをGETメソッドで受け取った時の処理
@app.route('/scraping', methods=['GET', 'POST'])
async def get():
    channel_id = request.args.get("channel-id", "")
    word = request.args.get("word", "")
    from_date = request.args.get("from", "")
    to_date = request.args.get("to", "")
    viewcount_level = int(request.args.get("viewcount-level", "") or 0)
    subscribercount_level = int(request.args.get("subscribercount-level", "") or 0)
    video_count = int(request.args.get("video-count", "") or 0)
    is_get_comment = request.args.get("comments", "")
    
    # 非同期関数を呼び出すためにawaitを使用
    sorce = await search_youtube.search_youtube(channel_id, word, from_date, to_date, viewcount_level, subscribercount_level, video_count, is_get_comment)
    
    print(datetime.datetime.now())
    if sorce is None:
        return await render_template('layout.html', title='search_youtube')

    if request.method == 'GET': # GETされたとき
        print('出力')
        return await render_template('template.html', sorce=sorce, is_get_comment=is_get_comment)
        
    elif request.method == 'POST': # POSTされたとき
        return 'POST'

# /commentをGETメソッドで受け取った時の処理
@app.route('/comment', methods=['GET', 'POST'])
async def get_comment():
    video_id = common.get_video_id(request.args.get("video-id", ""))
    
    # 非同期関数を呼び出すためにawaitを使用
    sorce = await get_comment_by_id.get_comment_by_id(video_id, '')
    
    print(datetime.datetime.now())
    if sorce is None:
        return await render_template('layout.html', title='search_youtube')

    if request.method == 'GET': # GETされたとき
        print('出力')
        return await render_template('comment.html', sorce=sorce)
        
    elif request.method == 'POST': # POSTされたとき
        return 'POST'

if __name__ == "__main__":
    app.run(debug=True)
