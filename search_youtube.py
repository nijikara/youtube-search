import aiohttp
import asyncio
import json
import datetime
import common
import os
from dotenv import load_dotenv
import urllib.parse
import requests
import get_comment

async def search_youtube(channel_id, key_word, published_from, published_to, viewcount_level, subscribercount_level, video_count, is_get_comment):
    # 開始時刻
    print(datetime.datetime.now())
    load_dotenv('.env') 

    if published_from == '':
        published_from = '2005-04-01'
    if published_to == '':
        published_to = str(datetime.date.today())

    url = os.environ.get("URL")
    api_key = os.environ.get("API_KEY")
    
    regionCode = 'JP'
    published_from += 'T00:00:00.000Z'
    published_to += 'T23:59:59.999Z'
    videoCategoryId = ''
    nextPageToken = ''
    buzz_lists_count = []
    outputs = []

    async with aiohttp.ClientSession() as session:
        while True:
            buzz_lists = []
            param = {
                'part': 'snippet',
                'q': key_word,
                'regionCode': regionCode,
                'maxResults': 50,
                'order': 'viewcount',
                'publishedAfter': published_from,
                'publishedBefore': published_to,
                'type': 'video',
                'channelId': channel_id,
                'pageToken': nextPageToken,
                'key': api_key
            }
            if videoCategoryId:
                param['videoCategoryId'] = videoCategoryId

            target_url = url + 'search?' + urllib.parse.urlencode(param)
            try:
                async with session.get(target_url) as response:
                    # ステータスコードをチェック
                    if response.status != 200:
                        error_message = f"API request failed with status {response.status}: {await response.text()}"
                        return [
                        {
                            'error': error_message
                        }]
                    search_body = await response.json()
                    video_list = [item['id']['videoId'] for item in search_body['items']]
                    channels_list = [item['snippet']['channelId'] for item in search_body['items']]
                    
                    # まずget_videoを実行し、それが完了した後にget_channelを呼び出す
                    await get_video(session, url, buzz_lists, video_list, api_key)
                    await get_channel(session, url, param, buzz_lists, channels_list, api_key)
                    
                    # 非同期でURLのリダイレクトを確認
                    video_urls = await get_video_urls(session, buzz_lists)
                    
                    # 条件を満たす buzz_list を一度に処理
                    valid_buzz_lists = [
                        {
                            'publishedAt': common.change_time(buzz_list['publishedAt']),
                            'title': buzz_list['title'], 
                            'description': buzz_list['description'], 
                            'viewCount': buzz_list['viewCount'], 
                            'likeCount': buzz_list['likeCount'], 
                            'commentCount': buzz_list['commentCount'], 
                            'videoDuration': buzz_list['videoDuration'], 
                            'thumbnails': buzz_list['thumbnails'], 
                            'video_url': video_urls[i],
                            'name': buzz_list.get('name', 'Unknown'), 
                            'subscriberCount': buzz_list.get('subscriberCount', 0), 
                            'channel_icon': [buzz_list.get('channel_url', ''), buzz_list.get('channel_icon', '')],
                            'comment': (await get_comment(session, api_key, buzz_list['video_id'], nextPageToken) if is_get_comment else [])
                        }
                        for i, buzz_list in enumerate(buzz_lists)
                        if int(buzz_list['viewCount']) >= viewcount_level and int(buzz_list['subscriberCount']) >= subscribercount_level
                    ]
                    buzz_lists_count.extend(valid_buzz_lists)
                    outputs.extend(valid_buzz_lists)

                print('出力結果'+str(len(buzz_lists_count))+'件')
                if len(buzz_lists_count) >= video_count:
                    return outputs

                if 'nextPageToken' in search_body:
                    nextPageToken = search_body['nextPageToken']
                else:
                    return outputs

            except aiohttp.ClientResponseError as err:
                print("エラー")
                print(err)
                break
            except aiohttp.ClientConnectorError as err:
                print("エラー")
                print(err)
                break

async def get_video(session, url, buzz_lists, video_list, api_key):
    param = {
        'part': 'snippet,statistics,contentDetails',
        'id': ",".join(video_list),
        'key': api_key
    }
    target_url = url + 'videos?' + urllib.parse.urlencode(param)
    try:
        async with session.get(target_url) as response:
            videos_body = await response.json()
            for i, item in enumerate(videos_body['items']):
                buzz_lists.append({})  # 空の辞書を追加しておく
                buzz_lists[i]['channelId'] = item['snippet']['channelId'] 
                buzz_lists[i]['title'] = item['snippet']['title'] 
                buzz_lists[i]['description'] = item['snippet']['description']
                buzz_lists[i]['viewCount'] = item['statistics']['viewCount']
                buzz_lists[i]['publishedAt'] = item['snippet']['publishedAt']
                buzz_lists[i]['thumbnails'] = item['snippet']['thumbnails']['high']['url']
                buzz_lists[i]['likeCount'] = item['statistics'].get('likeCount', 0)
                buzz_lists[i]['commentCount'] = item['statistics'].get('commentCount', 0)
                buzz_lists[i]['videoDuration'] = common.get_time(common.parse_duration(item['contentDetails']['duration']))
                buzz_lists[i]['video_id'] = item['id']

    except aiohttp.ClientResponseError as err:
        print('エラー')
        print(err)
        return False
    except aiohttp.ClientConnectorError as err:
        print('エラー')
        print(err)
        return False
    return True

async def get_channel(session, url, param, buzz_lists, channels_list, api_key):
    param = {
        'part': 'snippet,statistics',
        'id': ",".join(channels_list),
        'key': api_key
    }
    target_url = url + 'channels?' + urllib.parse.urlencode(param)

    try:
        async with session.get(target_url) as response:
            channels_body = await response.json()
            for i, buzz_list in enumerate(buzz_lists):
                list_search = [item for item in channels_body['items'] if item['id'] == buzz_list['channelId']]
                
                if list_search:
                    channel_info = list_search[0]
                    buzz_list['name'] = channel_info['snippet']['title']
                    buzz_list['subscriberCount'] = channel_info['statistics']['subscriberCount']
                    buzz_list['channel_url'] = 'https://www.youtube.com/channel/' + channel_info['id']
                    buzz_list['channel_icon'] = channel_info['snippet']['thumbnails']['default']['url']
                else:
                    print(f"Channel ID {buzz_list['channelId']} not found in response.")

    except aiohttp.ClientResponseError as err:
        print(err)
        return False
    except aiohttp.ClientConnectorError as err:
        print(err)
        return False
    return True

async def get_video_urls(session, buzz_lists):
    tasks = []
    for buzz_list in buzz_lists:
        shorts_url = 'https://www.youtube.com/shorts/' + buzz_list['video_id']
        watch_url = 'https://www.youtube.com/watch?v=' + buzz_list['video_id']
        tasks.append(check_redirect(session, shorts_url, watch_url))

    video_urls = await asyncio.gather(*tasks)
    return video_urls

async def check_redirect(session, shorts_url, watch_url):
    async with session.get(shorts_url, allow_redirects=True) as response:
        if len(response.history) == 0:
            return shorts_url
        else:
            return watch_url
