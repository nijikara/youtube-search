import urllib.request
import urllib.parse
import requests
import json
import datetime
import common
import os
from dotenv import load_dotenv

#-------↓パラメータ入力↓-------
def search_youtube(key_word,published_from,published_to,viewcount_level,subscribercount_level,video_count):
    # 開始時刻
    print(datetime.datetime.now())
    load_dotenv('.env') 

    if published_from == '':
        published_from = '2005-04-01'
    if published_to == '':
        published_to = str(datetime.date.today())

    url = os.environ.get("URL")
    api_key = os.environ.get("API_KEY")
    # key_word = 'ゲーム'
    regionCode = 'JP'
    # 検索日時from
    published_from += 'T00:00:00.000Z'
    # # 検索日時to
    published_to += 'T23:59:59.999Z'
    videoCategoryId = ''
    channelId = ''
    comment = []
    
    # 再生数下限
    # viewcount_level = 0
    # # 登録者数上限
    # subscribercount_level = 100000000
    # # 取得件数
    # video_count = 10000

    #-------↑パラメータ入力↑-------

    nextPageToken = ''
    buzz_lists_count = []
    outputs = []

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
            'publishedAfter':published_from,
            'publishedBefore':published_to,
            'type':'video',
            'channelId':channelId,
            'pageToken':nextPageToken,
            'key':api_key
        }
        #videoCategoryIdはブランクでパラメータを渡すとエラーになるので値がある時のみパラメータ付ける
        if not videoCategoryId:
            pass
        else:
            param['videoCategoryId'] = videoCategoryId
        #target_url:youtubeからのjson情報
        target_url = url + 'search?'+urllib.parse.urlencode(param)
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
                if get_video(url,buzz_lists,video_list,api_key) == False:
                    break
                    
                #channelメソッドで登録者数取得-----------------------------------------------------------------
                if get_channel(url,param,buzz_lists,channels_list,api_key) == False:
                    break

                #指定した再生回数以上 and 登録者数以下の場合のみCSVに吐く-----------------------------------------
                
                for buzz_list in buzz_lists:
                    # print(viewcount_level)
                    # print(buzz_list['viewCount'])

                    if( int(buzz_list['viewCount']) >= viewcount_level and int(buzz_list['subscriberCount']) >= subscribercount_level ):

                        #ショート動画の存在チェック
                        if not requests.get('https://www.youtube.com/shorts/' + buzz_list['video_id']).history:
                            video_url = 'https://www.youtube.com/shorts/' + buzz_list['video_id']
                        else:
                            video_url = 'https://www.youtube.com/watch?v=' + buzz_list['video_id']

                        # コメント取得
                        # comment = get_comment.get_comment(APIKEY,buzz_list['video_id'])
                        #CSV出力用
                        # outputs.append([
                        #     buzz_list['title'], 
                        #     buzz_list['description'], 
                        #     buzz_list['viewCount'], 
                        #     common.change_time(buzz_list['publishedAt']),
                        #     buzz_list['thumbnails'], 
                        #     video_url, 
                        #     buzz_list['name'], 
                        #     buzz_list['subscriberCount'], 
                        #     buzz_list['channel_url']
                        # # comment 
                        # ])
                        outputs.append({
                            'publishedAt':common.change_time(buzz_list['publishedAt']),
                            'title':buzz_list['title'], 
                            'description':buzz_list['description'], 
                            'viewCount':buzz_list['viewCount'], 
                            'likeCount':buzz_list['likeCount'], 
                            'commentCount':buzz_list['commentCount'], 
                            'thumbnails':buzz_list['thumbnails'], 
                            'video_url':video_url, 
                            'name':buzz_list['name'], 
                            'subscriberCount':buzz_list['subscriberCount'], 
                            'channel_icon':[buzz_list['channel_url'],buzz_list['channel_icon']]
                        # comment 
                        })
                        #ループ数管理用
                        buzz_lists_count.append(buzz_list)

            print("roop")
            #条件に合致する動画が必要数集まるまでループ-----------------------------------------
            # print(len(buzz_lists_count))
            if( len(buzz_lists_count) >= video_count ):
                print('条件に合致する動画が必要数集まる')
                return outputs

            #nextPageTokenが表示されなくなったらストップ
            if 'nextPageToken' in search_body:
                nextPageToken = search_body['nextPageToken']
            else:
                print('nextPageTokenが表示されなくなった')
                return outputs

        except urllib.error.HTTPError as err:
            print(err)
            break
        except urllib.error.URLError as err:
            print(err)
            break

# 動画情報取得
def get_video(url,buzz_lists,video_list,api_key):
    param = {
        'part':'snippet,statistics',
        'id':",".join(video_list),
        'key':api_key
    }
    target_url = url + 'videos?'+(urllib.parse.urlencode(param))
    req = urllib.request.Request(target_url)
    try:
        with urllib.request.urlopen(req) as res:
            videos_body = json.load(res)

            #出力用データに追記
            v = 0
            # print("item")
            # print(videos_body['items'])
            # print("statistics")
            # print(videos_body['statistics'])
            # print("likeCount")
            # print(videos_body['statistics']['likeCount'])

            for item in videos_body['items']:
                buzz_lists[v]['title'] = item['snippet']['title'] #タイトル
                buzz_lists[v]['description'] = item['snippet']['description'] #概要
                buzz_lists[v]['viewCount'] = item['statistics']['viewCount'] #再生数
                buzz_lists[v]['publishedAt'] = item['snippet']['publishedAt'] #投稿日時
                buzz_lists[v]['thumbnails'] = item['snippet']['thumbnails']['high']['url'] #サムネイル
                # buzz_lists[v]['likeCount'] = int(item['statistics']['likeCount'] or 0) #高評価数
                # なぜか高評価数がない場合がある
                if 'likeCount' in item['statistics'] :
                    buzz_lists[v]['likeCount'] = item['statistics']['likeCount'] #高評価数
                else:
                    buzz_lists[v]['likeCount'] = 0
                # なぜかコメント数がない場合がある
                if 'commentCount' in item['statistics'] :
                    buzz_lists[v]['commentCount'] = item['statistics']['commentCount'] #コメント数
                else:
                    buzz_lists[v]['commentCount'] = 0
                buzz_lists[v]['video_id'] = item['id'] #id
                v += 1

    except urllib.error.HTTPError as err:
        print(err)
        return False

    except urllib.error.URLError as err:
        print(err)
        return False
    return True

# チャンネル情報取得
def get_channel(url,param,buzz_lists,channels_list,api_key):
    param = {
        'part':'snippet,statistics',
        'id':",".join(channels_list),
        'key':api_key
    }
    target_url = url + 'channels?'+(urllib.parse.urlencode(param))
    print(target_url)
    req = urllib.request.Request(target_url)

    try:
        with urllib.request.urlopen(req) as res:
            channels_body = json.load(res)

            #出力用データに追記
            c = 0
            for buzz_list in buzz_lists:
                list_search = [ item for item in channels_body['items'] if item['id'] == buzz_list['channelId'] ]
                buzz_lists[c]['name'] = list_search[0]['snippet']['title'] #チャンネル名
                buzz_lists[c]['subscriberCount'] = list_search[0]['statistics']['subscriberCount'] #登録者数
                buzz_lists[c]['channel_url'] = 'https://www.youtube.com/channel/'+list_search[0]['id'] #チャンネルURL
                buzz_lists[c]['channel_icon'] = list_search[0]['snippet']['thumbnails']['default']['url'] #チャンネルアイコン
                c += 1

    except urllib.error.HTTPError as err:
        print(err)
        return False
    except urllib.error.URLError as err:
        print(err)
        return False
    return True