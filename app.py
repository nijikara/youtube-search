from flask import Flask, render_template
from flask import request
import datetime
import get_comment_by_id
import search_youtube
# from flask import Markup

app = Flask(__name__, static_folder='./templates/images')

@app.route('/')
def hello():
    return render_template('layout.html', title='search_youtube')

# ↓ /scrapingをGETメソッドで受け取った時の処理
@app.route('/scraping', methods=['GET', 'POST'])
def get():
    channel_id = request.args.get("channel-id","")
    word = request.args.get("word","")
    from_date = request.args.get("from","")
    to_date = request.args.get("to","")
    viewcount_level = int(request.args.get("viewcount-level","") or 0)
    subscribercount_level = int(request.args.get("subscribercount-level","") or 0)
    video_count = int(request.args.get("video-count","") or 0)
    is_get_comment = request.args.get("comments","")
    sorce = search_youtube.search_youtube(channel_id,word,from_date,to_date,viewcount_level,subscribercount_level,video_count,is_get_comment)
    # print(sorce)
    print(datetime.datetime.now())
    if sorce == None:
        return render_template('layout.html', title='search_youtube')

    if request.method == 'GET': # GETされたとき
        print('出力')
        # sorce = Markup(sorce)
        return render_template('template.html',sorce = sorce,is_get_comment = is_get_comment)
        
    elif request.method == 'POST': # POSTされたとき
        return 'POST'

# ↓ /commentをGETメソッドで受け取った時の処理
@app.route('/comment', methods=['GET', 'POST'])
def get_comment():
    video_id = request.args.get("video-id","").replace("https://youtu.be/","")  # リクエストからvideo-idを取得

    print(video_id)
    sorce = get_comment_by_id.get_comment_by_id(video_id,'') 
    # print(sorce)
    print(datetime.datetime.now())
    if sorce == None:
        return render_template('layout.html', title='search_youtube')

    if request.method == 'GET': # GETされたとき
        print('出力')
        # sorce = Markup(sorce)
        # print(sorce)
        return render_template('comment.html',sorce = sorce)
        
    elif request.method == 'POST': # POSTされたとき
        return 'POST'


if __name__ == "__main__":
    app.run(debug=True)