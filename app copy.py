import urllib.request
import urllib.parse
import requests
import json
import csv
import datetime

#-------↓パラメータ入力↓-------

APIKEY = 'AIzaSyD-0yP4hiqHw9veXw4D6SOrJRsSl8HRMRs'
key_word = 'ゲーム'
regionCode = 'JP'
publishedAfter = '2022-11-14T00:00:00.000Z'
publishedBefore = '2022-11-22T00:00:00.000Z'
viewcount_level = 0
subscribercount_level = 5000
video_count = 1
videoCategoryId = ''
channelId = ''

#-------↑パラメータ入力↑-------

dt_now = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
nextPageToken = ''
buzz_lists_count = []
csv_outputs_header = []
csv_outputs_header.append(['title', 'description', 'viewCount', 'publishedAt', 'thumbnail', 'video_url', 'name', 'subscriberCount', 'channel_url'])

#書き込み用CSV開く
with open(dt_now + '_youtube-buzz-list.csv', 'w', newline='', encoding='UTF-8') as f:
    writer = csv.writer(f)
    writer.writerows(csv_outputs_header)

    #無限ループ
    while True:

        buzz_lists = []
        #searchメソッドで動画ID一覧取得
        param = {
            'part':'snippet',
            'q':key_word,
            'regionCode':regionCode,
            'maxResults':50,
            'order':'viewcount',
            'publishedAfter':publishedAfter,
            'publishedBefore':publishedBefore,
            'type':'video',
            'channelId':channelId,
            'pageToken':nextPageToken,
            'key':APIKEY
        }
        #videoCategoryIdはブランクでパラメータを渡すとエラーになるので値がある時のみパラメータ付ける
        if not videoCategoryId:
            pass
        else:
            param['videoCategoryId'] = videoCategoryId

        target_url = 'https://www.googleapis.com/youtube/v3/search?'+urllib.parse.urlencode(param)
        print(target_url)
        req = urllib.request.Request(target_url)
        try:
            with urllib.request.urlopen(req) as res:

                search_body = json.load(res)
                video_list = []
                channels_list = []

                for item in search_body['items']:
                    #videoメソッド用list作成
                    video_list.append(item['id']['videoId'])
                    channels_list.append(item['snippet']['channelId'])
                    #出力用データに追記
                    buzz_lists.append( {'videoId':item['id']['videoId'], 'channelId':item['snippet']['channelId']} )
                    
                #videoメソッドで動画情報取得-----------------------------------------------------------------
                param = {
                    'part':'snippet,statistics',
                    'id':",".join(video_list),
                    'key':APIKEY
                }
                target_url = 'https://www.googleapis.com/youtube/v3/videos?'+(urllib.parse.urlencode(param))
                req = urllib.request.Request(target_url)
                print(target_url)
                try:
                    with urllib.request.urlopen(req) as res:
                        videos_body = json.load(res)

                        #出力用データに追記
                        v = 0
                        for item in videos_body['items']:
                            buzz_lists[v]['title'] = item['snippet']['title']
                            buzz_lists[v]['description'] = item['snippet']['description']
                            buzz_lists[v]['viewCount'] = item['statistics']['viewCount']
                            buzz_lists[v]['publishedAt'] = item['snippet']['publishedAt']
                            buzz_lists[v]['thumbnails'] = item['snippet']['thumbnails']['high']['url']
                            buzz_lists[v]['video_id'] = item['id']
                            v += 1

                except urllib.error.HTTPError as err:
                    print(err)
                    break
                except urllib.error.URLError as err:
                    print(err)
                    break
                    
                #channelメソッドで登録者数取得-----------------------------------------------------------------
                param = {
                    'part':'snippet,statistics',
                    'id':",".join(channels_list),
                    'key':APIKEY
                }
                target_url = 'https://www.googleapis.com/youtube/v3/channels?'+(urllib.parse.urlencode(param))
                print(target_url)
                req = urllib.request.Request(target_url)

                try:
                    with urllib.request.urlopen(req) as res:
                        channels_body = json.load(res)

                        #出力用データに追記
                        c = 0
                        for buzz_list in buzz_lists:
                            list_search = [ item for item in channels_body['items'] if item['id'] == buzz_list['channelId'] ]
                            buzz_lists[c]['name'] = list_search[0]['snippet']['title']
                            buzz_lists[c]['subscriberCount'] = list_search[0]['statistics']['subscriberCount']
                            buzz_lists[c]['channel_url'] = 'https://www.youtube.com/channel/'+list_search[0]['id']
                            c += 1

                except urllib.error.HTTPError as err:
                    print(err)
                    break
                except urllib.error.URLError as err:
                    print(err)
                    break

                #指定した再生回数以上 and 登録者数以下の場合のみCSVに吐く-----------------------------------------
                csv_outputs = []
                for buzz_list in buzz_lists:

                    if( int(buzz_list['viewCount']) >= viewcount_level and int(buzz_list['subscriberCount']) <= subscribercount_level ):

                        #ショート動画の存在チェック
                        if not requests.get('https://www.youtube.com/shorts/' + buzz_list['video_id']).history:
                            print('ショートよ')
                            video_url = 'https://www.youtube.com/shorts/' + buzz_list['video_id']
                        else:
                            print('ショートじゃなーい')
                            video_url = 'https://www.youtube.com/watch?v=' + buzz_list['video_id']

                        #CSV出力用
                        csv_outputs.append([buzz_list['title'], buzz_list['description'], buzz_list['viewCount'], buzz_list['publishedAt'], buzz_list['thumbnails'], video_url, buzz_list['name'], buzz_list['subscriberCount'], buzz_list['channel_url'] ])
                        #ループ数管理用
                        buzz_lists_count.append(buzz_list)

                #CSV追記
                writer.writerows( csv_outputs )

            #条件に合致する動画が必要数集まるまでループ-----------------------------------------
            print(len(buzz_lists_count))
            if( len(buzz_lists_count) >= video_count ):
                break

            #nextPageTokenが表示されなくなったらストップ
            if 'nextPageToken' in search_body:
                nextPageToken = search_body['nextPageToken']
            else:
                break

        except urllib.error.HTTPError as err:
            print(err)
            break
        except urllib.error.URLError as err:
            print(err)
            break