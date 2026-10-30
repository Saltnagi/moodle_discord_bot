import os
import subprocess
import sys
import pytest
from single_instance import SingleInstanceLock, AlreadyRunningError

def test_single_instance_reentrant(tmp_path):
    lock_file = str(tmp_path / "test_reentrant.lock")

    lock1 = SingleInstanceLock(lock_file)
    lock1.acquire()

    # 同一プロセス内では再入として扱われる
    lock2 = SingleInstanceLock(lock_file)
    lock2.acquire()
    lock2.release()
    lock1.release()

def test_single_instance_cross_process(tmp_path):
    lock_file = str(tmp_path / "test_subproc.lock")

    cmd = [
        sys.executable,
        "-c",
        f"from single_instance import SingleInstanceLock; import time; lock = SingleInstanceLock(r'{lock_file}'); lock.acquire(); print('READY', flush=True); time.sleep(1.5)",
    ]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True)
    line = proc.stdout.readline()
    assert line.strip() == "READY"

    try:
        lock2 = SingleInstanceLock(lock_file)
        with pytest.raises(AlreadyRunningError):
            lock2.acquire()
    finally:
        proc.wait()
