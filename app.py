from flask import Flask, render_template
from flask import request
import datetime
import search_youtube
from flask import Markup

app = Flask(__name__, static_folder='./templates/images')

@app.route('/')
def hello():
    return render_template('layout.html', title='search_youtube')

# ↓ /scrapingをGETメソッドで受け取った時の処理
@app.route('/scraping', methods=['GET', 'POST'])
def get():
    word = request.args.get("word","")
    from_date = request.args.get("from","")
    to_date = request.args.get("to","")
    viewcount_level = int(request.args.get("viewcount-level","") or 0)
    subscribercount_level = int(request.args.get("subscribercount-level","") or 0)
    video_count = int(request.args.get("video-count","") or 0)
    sorce = search_youtube.search_youtube(word,from_date,to_date,viewcount_level,subscribercount_level,video_count)
    print(len(sorce))
    print(datetime.datetime.now())
    if request.method == 'GET': # GETされたとき
        print('出力')
        # sorce = Markup(sorce)
        return render_template('template.html',sorce = sorce)
        
    elif request.method == 'POST': # POSTされたとき
        return 'POST'


if __name__ == "__main__":
    app.run(debug=True)