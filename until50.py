import os

import googleapiclient.discovery as build

DEVELOPER_KEY = 'AIzaSyD-0yP4hiqHw9veXw4D6SOrJRsSl8HRMRs'
YOUTUBE_API_SERVICE_NAME = 'youtube'
YOUTUBE_API_VERSION = 'v3'


def get_video_info(keyword):
    # search settings
    youtube_query = youtube.search().list(
        part='id,snippet',
        q=keyword,
        type='video',
        maxResults=50,
        order='relevance',
    )

    # execute()で検索を実行
    youtube_response = youtube_query.execute()

    # 検索結果を取得し、リターンする
    return youtube_response.get('items', [])

youtube = build.build(YOUTUBE_API_SERVICE_NAME, YOUTUBE_API_VERSION, developerKey=DEVELOPER_KEY)


# 先程作った関数を実行
data = get_video_info("泥ママ")

# 取得したデータから5件分の動画タイトルをforループで出力します。
for video in data:
    print(video['snippet']['title'])