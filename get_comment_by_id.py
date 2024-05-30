import requests
import os
from dotenv import load_dotenv
import common

def get_comment_by_id(video_id, next_page_token):
  print(video_id)
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
  no = 0

  for comment_info in resource['items']:
    # print(comment_info)
    # 日時
    publishedAt = comment_info['snippet']['topLevelComment']['snippet']['publishedAt']
    # コメント
    text = comment_info['snippet']['topLevelComment']['snippet']['textDisplay']
    # # グッド数
    like_cnt = comment_info['snippet']['topLevelComment']['snippet']['likeCount']
    # # 返信数
    reply_cnt = comment_info['snippet']['totalReplyCount']
    # # ユーザー名
    user_name = comment_info['snippet']['topLevelComment']['snippet']['authorDisplayName']
    # # Id
    parentId = comment_info['snippet']['topLevelComment']['id']
    comments.append({
      # 'comment':text.replace('\r', '\n').replace('\n', ' '),
      'publishedAt':common.change_time(publishedAt),
      'comment':text,
      'like_cnt':like_cnt,
      'reply_cnt':reply_cnt,
      'user_name':user_name,
      'parentId':parentId + '-0',

      })



    if reply_cnt > 0:
      print(f"リプライ{reply_cnt}")
      cno = 0
      print_video_reply(no, cno, video_id, None, parentId,api_key,comments)
    no = no + 1

  # if 'nextPageToken' in resource:
  #   print("再取得")
  #   get_comment_by_id(video_id, resource["nextPageToken"])
  return comments

def print_video_reply(no, cno, video_id, next_page_token, parentId,api_key,comments):
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
  url = os.environ.get("URL")
  response = requests.get(url + 'comments', params=params)
  resource = response.json()

  for comment_info in resource['items']:
    # 日時
    publishedAt = comment_info['snippet']['publishedAt']
    # コメント
    text = comment_info['snippet']['textDisplay']
    # グッド数
    like_cnt = comment_info['snippet']['likeCount']
    # ユーザー名
    user_name = comment_info['snippet']['authorDisplayName']

    cno = cno + 1
    comments.append({
      'publishedAt':common.change_time(publishedAt),
      'comment':text.replace('\r', '\n').replace('\n', ' '),
      'like_cnt':like_cnt,
      'reply_cnt':0,
      'user_name':user_name,
      'parentId':parentId + '-' +str(cno),

      })

  if 'nextPageToken' in resource:
    print_video_reply(no, cno, video_id, resource["nextPageToken"], id,api_key,comments)
