from enum import Enum


class FailureKind(Enum):
    TRANSIENT = "transient"  # タイムアウト/接続失敗/5xx/429/サイズ超過
    INVALID = "invalid"      # 401/403/404/410、ICSではない応答（ログインHTML等）
    SUSPECT = "suspect"      # 200/2xx、ICSではない応答（ログインHTML等）


class MoodleError(Exception):
    def __init__(self, message: str, kind: FailureKind = FailureKind.TRANSIENT):
        super().__init__(message)
        self.kind = kind
