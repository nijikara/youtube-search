from flask import Flask, render_template
from flask import request
import datetime
import search_youtube
from flask import Markup

app = Flask(__name__)

@app.route('/')
def hello():
    return render_template('layout.html', title='twitter_get')

# ↓ /scrapingをGETメソッドで受け取った時の処理
@app.route('/scraping', methods=['GET', 'POST'])
def get():
    field = request.args.get("field","")
    fromDate = request.args.get("from","")
    toDate = request.args.get("to","")
    today = datetime.datetime.now()
    sorce = search_youtube.search_youtube(field,fromDate,toDate)
    if request.method == 'GET': # GETされたとき
        print('出力')
        sorce = Markup(sorce)
        return render_template('template.html',sorce = sorce)
        
    elif request.method == 'POST': # POSTされたとき
        return 'POST'


if __name__ == "__main__":
    app.run(debug=True)