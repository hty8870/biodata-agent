"""脚本子进程单一通道：超时、取消与退出均回收本次拥有的进程树。

POSIX 使用独立进程组；Windows 使用 kill-on-close Job。Windows 引导进程先等
stdin 放行，再启动实际命令，确保命令及后代都在 Job 内，消除启动后才绑 Job 的竞态。
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path


class _WindowsJob:
    def __init__(self):
        import ctypes as c
        from ctypes import wintypes as w

        class Limits(c.Structure):
            _fields_ = [("process_time", c.c_longlong), ("job_time", c.c_longlong),
                        ("flags", w.DWORD), ("min_ws", c.c_size_t), ("max_ws", c.c_size_t),
                        ("active", w.DWORD), ("affinity", c.c_size_t),
                        ("priority", w.DWORD), ("scheduling", w.DWORD)]

        class ExtendedLimits(c.Structure):
            _fields_ = [("basic", Limits), ("io", c.c_ulonglong * 6),
                        ("process_memory", c.c_size_t), ("job_memory", c.c_size_t),
                        ("peak_process", c.c_size_t), ("peak_job", c.c_size_t)]

        self.api = c.WinDLL("kernel32", use_last_error=True)
        self.api.CreateJobObjectW.argtypes = [c.c_void_p, w.LPCWSTR]
        self.api.CreateJobObjectW.restype = w.HANDLE
        self.api.SetInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
        self.api.SetInformationJobObject.restype = w.BOOL
        self.api.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        self.api.AssignProcessToJobObject.restype = w.BOOL
        self.api.CloseHandle.argtypes = [w.HANDLE]
        self.api.CloseHandle.restype = w.BOOL
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise c.WinError(c.get_last_error())
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.api.SetInformationJobObject(self.handle, 9, c.byref(limits), c.sizeof(limits)):
            error = c.WinError(c.get_last_error())
            self.close()
            raise error

    def attach(self, process):
        import ctypes
        if not self.api.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


def run(command, *, timeout=None, capture_output=False, check=False, **kwargs):
    """subprocess.run 的有界脚本用法；stdin 由启动闸独占，不接交互输入。"""
    if "stdin" in kwargs or "input" in kwargs:
        raise ValueError("process-tree runner does not accept interactive input")
    if capture_output:
        if "stdout" in kwargs or "stderr" in kwargs:
            raise ValueError("capture_output conflicts with stdout/stderr")
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    windows = os.name == "nt"
    job = _WindowsJob() if windows else None
    argv = [os.fspath(arg) for arg in command]
    actual = ([sys.executable, "-B", str(Path(__file__).resolve()), "--launch", json.dumps(argv)]
              if windows else argv)
    if windows:
        kwargs["stdin"] = subprocess.PIPE
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | subprocess.CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    process = None

    def stop():
        if job is not None:
            job.close()
        elif process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    try:
        process = subprocess.Popen(actual, **kwargs)
        try:
            if job is not None:
                job.attach(process)
            release = ("G" if process.text_mode else b"G") if windows else None
            stdout, stderr = process.communicate(input=release, timeout=timeout)
        except BaseException as exc:
            stop()
            # Job 绑定失败时引导进程仍在等放行，只需结束该进程，尚无真实命令。
            if process.poll() is None:
                process.kill()
            stdout, stderr = process.communicate()
            if isinstance(exc, subprocess.TimeoutExpired):
                raise subprocess.TimeoutExpired(command, timeout, output=stdout, stderr=stderr) from None
            raise
        result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        if check:
            result.check_returncode()
        return result
    finally:
        stop()
        if process is not None:
            process.wait()


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--launch":
        raise SystemExit("This module is an internal subprocess helper.")
    if sys.stdin.buffer.read(1) != b"G":
        raise SystemExit(2)
    raise SystemExit(subprocess.call(json.loads(sys.argv[2]), stdin=subprocess.DEVNULL))
