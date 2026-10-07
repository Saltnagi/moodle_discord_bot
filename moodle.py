# Moodleサーバとの通信、.icsファイルの解析
import datetime
from icalendar import Calendar
import requests

def get_moodle_events(ics_url: str, filter_words: list):
    """Moodleから予定を取得してフィルタリングした文字列リストを返す"""
    try:
        response = requests.get(ics_url)
        if response.status_code != 200:
            return ["Moodleからのデータ取得に失敗しました。"]

        # デバッグログ
        print(f"--- Moodle通信成功 (HTTP {response.status_code}) ---")
        
        cal = Calendar.from_ical(response.content)
        events = []

        for component in cal.walk():
            if component.name == "VEVENT":
                summary = component.get("summary")
                dtstart_obj = component.get("dtstart")

                # 日時の変換処理
                if dtstart_obj:
                    dt = dtstart_obj.dt
                    if isinstance(dt, datetime.datetime):
                        start_str = dt.strftime("%Y-%m-%d %H:%M")
                    elif isinstance(dt, datetime.date):
                        start_str = dt.strftime("%Y-%m-%d")
                    else:
                        start_str = "日時不明"
                else:
                    start_str = "日時不明"

                summary_str = str(summary) if summary else ""
                # フィルターキーワードに一致する場合のみ判定
                if any(word in summary_str for word in filter_words):
                    events.append(f"・[{start_str}] {summary_str}")

        if not events:
            return ["現在、登録されている予定はありません。"]

        return events

    except Exception as e:
        return [f"エラーが発生しました: {e}"]