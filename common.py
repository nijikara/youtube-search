import datetime, pytz, time
import isodate
def change_time(created_at):
    st = time.strptime(str(created_at), '%Y-%m-%dT%H:%M:%S%z')        # time.struct_timeに変換
    utc_time = datetime.datetime(st.tm_year, st.tm_mon,st.tm_mday, \
        st.tm_hour,st.tm_min,st.tm_sec, tzinfo=datetime.timezone.utc)   # datetimeに変換(timezoneを付与)
    jst_time = utc_time.astimezone(pytz.timezone("Asia/Tokyo"))         # 日本時間に変換
    str_time = jst_time.strftime("%Y-%m-%d %H:%M:%S")                     # 文字列で返す
    return str_time

# 「x(秒)」から「x:xx:xx」形式に変更
def get_time(td):
    td = datetime.timedelta(seconds=td)
    m, s = divmod(td.seconds, 60)
    h, m = divmod(m, 60)
    if h == 0:  # 1時間未満であれば時間表記は省略
        return f'{m}:{s}'
    return f'{h}:{m}:{s}'

# isoから秒に変換
def parse_duration(duration):
    td = isodate.parse_duration(duration)
    return  td.total_seconds()