import aiohttp
import asyncio
import os
from dotenv import load_dotenv
import common
import datetime

async def fetch(session, url, params):
    async with session.get(url, params=params) as response:
        return await response.json()

async def get_comment_by_id(video_id):
    print(datetime.datetime.now())
    load_dotenv('.env')
    url = os.environ.get("URL") + 'commentThreads'
    api_key = os.environ.get("API_KEY")
    params = {
        'key': api_key,
        'part': 'snippet',
        'videoId': video_id,
        'order': 'relevance',
        'textFormat': 'plaintext',
        'maxResults': 100,
    }

    comments = []
    no = 1
    next_page_token = None

    async with aiohttp.ClientSession() as session:
        while True:
            if next_page_token:
                params['pageToken'] = next_page_token
            resource = await fetch(session, url, params)

            for comment_info in resource['items']:
                publishedAt = comment_info['snippet']['topLevelComment']['snippet']['publishedAt']
                text = comment_info['snippet']['topLevelComment']['snippet']['textDisplay']
                like_cnt = comment_info['snippet']['topLevelComment']['snippet']['likeCount']
                reply_cnt = comment_info['snippet']['totalReplyCount']
                user_name = comment_info['snippet']['topLevelComment']['snippet']['authorDisplayName']
                user_img = comment_info['snippet']['topLevelComment']['snippet']['authorProfileImageUrl']
                user_url = comment_info['snippet']['topLevelComment']['snippet']['authorChannelUrl']
                parentId = comment_info['snippet']['topLevelComment']['id']
                comments.append({
                    'no': str(no),
                    'publishedAt': common.change_time(publishedAt),
                    'comment': text,
                    'like_cnt': like_cnt,
                    'reply_cnt': reply_cnt,
                    'user_name': user_name,
                    'user_img': user_img,
                    'user_url': user_url,
                    'parentId': parentId,
                })

                if reply_cnt > 0:
                    cno = 0
                    await print_video_reply(no, cno, video_id, None, parentId, api_key, comments, session)
                no += 1

            if 'nextPageToken' in resource:
                next_page_token = resource['nextPageToken']
            else:
                break

    return comments

async def print_video_reply(no, cno, video_id, next_page_token, parentId, api_key, comments, session):
    url = os.environ.get("URL") + 'comments'
    while True:
        params = {
            'key': api_key,
            'part': 'snippet',
            'videoId': video_id,
            'textFormat': 'plaintext',
            'maxResults': 50,
            'parentId': parentId,
        }

        if next_page_token:
            params['pageToken'] = next_page_token
        resource = await fetch(session, url, params)

        for comment_info in resource['items']:
            publishedAt = comment_info['snippet']['publishedAt']
            text = comment_info['snippet']['textDisplay']
            like_cnt = comment_info['snippet']['likeCount']
            user_name = comment_info['snippet']['authorDisplayName']
            user_img = comment_info['snippet']['authorProfileImageUrl']
            user_url = comment_info['snippet']['authorChannelUrl']

            cno += 1
            comments.append({
                'no': str(no) + '-' + str(cno),
                'publishedAt': common.change_time(publishedAt),
                'comment': text.replace('\r', '\n').replace('\n', ' '),
                'like_cnt': like_cnt,
                'reply_cnt': 0,
                'user_name': user_name,
                'user_img': user_img,
                'user_url': user_url,
                'parentId': parentId,
            })

        if 'nextPageToken' in resource:
            next_page_token = resource["nextPageToken"]
        else:
            break
