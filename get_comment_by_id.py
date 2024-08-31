import requests
import os
from dotenv import load_dotenv
import common

async def get_comment_by_id(video_id):
    load_dotenv('.env') 

    url = os.environ.get("URL")
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

    while True:
        if next_page_token is not None:
            params['pageToken'] = next_page_token
        response = requests.get(url + 'commentThreads', params=params)
        resource = response.json()

        for comment_info in resource['items']:
            publishedAt = comment_info['snippet']['topLevelComment']['snippet']['publishedAt']
            text = comment_info['snippet']['topLevelComment']['snippet']['textDisplay']
            like_cnt = comment_info['snippet']['topLevelComment']['snippet']['likeCount']
            reply_cnt = comment_info['snippet']['totalReplyCount']
            user_name = comment_info['snippet']['topLevelComment']['snippet']['authorDisplayName']
            parentId = comment_info['snippet']['topLevelComment']['id']
            comments.append({
                'no': str(no),
                'publishedAt': common.change_time(publishedAt),
                'comment': text,
                'like_cnt': like_cnt,
                'reply_cnt': reply_cnt,
                'user_name': user_name,
                'parentId': parentId + '-0',
            })

            if reply_cnt > 0:
                print(f"リプライ{reply_cnt}")
                cno = 0
                print_video_reply(no, cno, video_id, None, parentId, api_key, comments)
            no = no + 1

        if 'nextPageToken' in resource:
            next_page_token = resource['nextPageToken']
        else:
            break

    return comments

def print_video_reply(no, cno, video_id, next_page_token, parentId, api_key, comments):
    url = os.environ.get("URL")
    while True:
        params = {
            'key': api_key,
            'part': 'snippet',
            'videoId': video_id,
            'textFormat': 'plaintext',
            'maxResults': 50,
            'parentId': parentId,
        }

        if next_page_token is not None:
            params['pageToken'] = next_page_token
        response = requests.get(url + 'comments', params=params)
        resource = response.json()

        for comment_info in resource['items']:
            publishedAt = comment_info['snippet']['publishedAt']
            text = comment_info['snippet']['textDisplay']
            like_cnt = comment_info['snippet']['likeCount']
            user_name = comment_info['snippet']['authorDisplayName']

            cno = cno + 1
            comments.append({
                'no': str(no) + '-' + str(cno),
                'publishedAt': common.change_time(publishedAt),
                'comment': text.replace('\r', '\n').replace('\n', ' '),
                'like_cnt': like_cnt,
                'reply_cnt': 0,
                'user_name': user_name,
                'parentId': parentId,
            })

        if 'nextPageToken' in resource:
            next_page_token = resource["nextPageToken"]
        else:
            break
