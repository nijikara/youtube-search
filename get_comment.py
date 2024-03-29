import requests
import os
from dotenv import load_dotenv

def print_video_comment(url,no, video_id, next_page_token):
      
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
  if next_page_token is not None:
    params['pageToken'] = next_page_token
  response = requests.get(url + 'commentThreads', params=params)
  resource = response.json()
  comments = []

  for comment_info in resource['items']:
    # コメント
    text = comment_info['snippet']['topLevelComment']['snippet']['textDisplay']
    # # グッド数
    # like_cnt = comment_info['snippet']['topLevelComment']['snippet']['likeCount']
    # # 返信数
    # reply_cnt = comment_info['snippet']['totalReplyCount']
    # # ユーザー名
    # user_name = comment_info['snippet']['topLevelComment']['snippet']['authorDisplayName']
    # # Id
    # parentId = comment_info['snippet']['topLevelComment']['id']
    comments.append(text.replace('\r', '\n').replace('\n', ' '))
    # comments.append(text)
    # if reply_cnt > 0:
    #   cno = 1
    #   print_video_reply(no, cno, video_id, None, parentId)
    no = no + 1

  if 'nextPageToken' in resource:
    print_video_comment(no, video_id, resource["nextPageToken"])
  return comments

def print_video_reply(no, cno, video_id, next_page_token, id,api_key):
  params = {
    'key': api_key,
    'part': 'snippet',
    'videoId': video_id,
    'textFormat': 'plaintext',
    'maxResults': 50,
    'parentId': id,
  }

  if next_page_token is not None:
    params['pageToken'] = next_page_token
  url = os.environ.get("URL")
  response = requests.get(url + 'comments', params=params)
  resource = response.json()

  for comment_info in resource['items']:
    # コメント
    text = comment_info['snippet']['textDisplay']
    # グッド数
    like_cnt = comment_info['snippet']['likeCount']
    # ユーザー名
    user_name = comment_info['snippet']['authorDisplayName']

    cno = cno + 1

  if 'nextPageToken' in resource:
    print_video_reply(no, cno, video_id, resource["nextPageToken"], id,api_key)

def get_comment(api_key,video_id):
    # コメントを全取得する
    print("ssssss")
    no = 1
    return print_video_comment(no, video_id, None)