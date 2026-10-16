import os
import threading


class AlreadyRunningError(RuntimeError):
    pass


_registry_lock = threading.Lock()
# 取得中のロック（同一プロセス内での再入判定用）。キーはロックファイルの絶対パス
_registry: dict = {}


class SingleInstanceLock:
    """同一ホスト上での多重起動を防止するプロセス排他ロック。

    - Linux/POSIX: fcntl.flock (LOCK_EX | LOCK_NB)
    - Windows: msvcrt.locking (LK_NBLCK)
    ロックファイル自体は削除せず、OSがプロセスの終了時に自動解放する。
    同一プロセス内で同じパスを再度 acquire した場合は、エラーにせず再入として扱う。
    """

    def __init__(self, path: str):
        self.path = path
        self._key = os.path.abspath(path)
        self._fp = None
        self._owner = None      # 再入の場合: 実際にロックを保持しているインスタンス
        self._refcount = 0      # 実際の保持者のみ使用: 再入している数

    def acquire(self) -> None:
        with _registry_lock:
            owner = _registry.get(self._key)
            if owner is not None:
                owner._refcount += 1
                self._owner = owner
                return
        self._acquire_os_lock()
        with _registry_lock:
            _registry[self._key] = self
            self._refcount = 0

    def release(self) -> None:
        with _registry_lock:
            if self._owner is not None:      # 再入分の解放
                self._owner._refcount -= 1
                self._owner = None
                return
            if self._refcount > 0:           # 再入者が残っている間は解放しない
                return
            _registry.pop(self._key, None)
        self._release_os_lock()

    def _acquire_os_lock(self) -> None:
        parent_dir = os.path.dirname(self.path)
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)
        try:
            # 読み書き両用で開き、ロックをかける
            self._fp = open(self.path, "a+b")
        except OSError as e:
            raise AlreadyRunningError(f"ロックファイルを開けませんでした: {e}") from e

        if os.name == "nt":
            import msvcrt
            try:
                self._fp.seek(0)
                msvcrt.locking(self._fp.fileno(), msvcrt.LK_NBLCK, 1)
            except (OSError, IOError) as e:
                self._fp.close()
                self._fp = None
                raise AlreadyRunningError("既に別のプロセスで起動中です。") from e
        else:
            import fcntl
            try:
                fcntl.flock(self._fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (OSError, IOError) as e:
                self._fp.close()
                self._fp = None
                raise AlreadyRunningError("既に別のプロセスで起動中です。") from e

        # ロック取得成功: 自プロセスのPIDを情報として書き込む
        try:
            self._fp.seek(0)
            self._fp.truncate(0)
            self._fp.write(f"{os.getpid()}\n".encode("utf-8"))
            self._fp.flush()
        except Exception:
            pass

    def _release_os_lock(self) -> None:
        if self._fp is not None:
            try:
                if os.name == "nt":
                    import msvcrt
                    self._fp.seek(0)
                    msvcrt.locking(self._fp.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self._fp.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            finally:
                try:
                    self._fp.close()
                except Exception:
                    pass
                self._fp = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()