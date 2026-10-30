# logging_setup.py
import logging
from logging.handlers import TimedRotatingFileHandler
import os
import sys

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging(log_dir: str, keep_days: int = 7) -> None:
    """
    - log_dir を自動作成
    - moodle_bot.log に日次ローテーション（when="midnight", backupCount=keep_days）で出力
    - 標準出力にも StreamHandler で出力
    - UTF-8エンコーディング指定
    """
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "moodle_bot.log")

    formatter = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT)

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    # 既存のハンドラーが重複追加されないようにクリア
    for h in list(root.handlers):
        root.removeHandler(h)

    # 日次ローテーションファイルハンドラー
    file_handler = TimedRotatingFileHandler(
        log_path,
        when="midnight",
        interval=1,
        backupCount=keep_days,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # コンソール出力ハンドラー
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)
