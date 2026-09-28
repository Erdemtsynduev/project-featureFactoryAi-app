"""Owned Windows Job Object. Children cannot execute payload until containment succeeds."""

import ctypes
import os
import sys
import time
from ctypes import wintypes

if sys.platform == "win32":

    class BasicLimits(ctypes.Structure):
        _fields_ = [
            ("process_time", ctypes.c_int64),
            ("job_time", ctypes.c_int64),
            ("flags", wintypes.DWORD),
            ("minimum", ctypes.c_size_t),
            ("maximum", ctypes.c_size_t),
            ("processes", wintypes.DWORD),
            ("affinity", ctypes.c_size_t),
            ("priority", wintypes.DWORD),
            ("scheduling", wintypes.DWORD),
        ]

    class IoCounters(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_uint64)
            for name in (
                "read_ops",
                "write_ops",
                "other_ops",
                "read_bytes",
                "write_bytes",
                "other_bytes",
            )
        ]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("basic", BasicLimits),
            ("io", IoCounters),
            ("process_memory", ctypes.c_size_t),
            ("job_memory", ctypes.c_size_t),
            ("peak_process", ctypes.c_size_t),
            ("peak_job", ctypes.c_size_t),
        ]

    class CpuLimits(ctypes.Structure):
        _fields_ = [("flags", wintypes.DWORD), ("rate", wintypes.DWORD)]

    class Accounting(ctypes.Structure):
        _fields_ = [
            ("user", ctypes.c_int64),
            ("kernel", ctypes.c_int64),
            ("period_user", ctypes.c_int64),
            ("period_kernel", ctypes.c_int64),
            ("faults", wintypes.DWORD),
            ("total", wintypes.DWORD),
            ("active", wintypes.DWORD),
            ("terminated", wintypes.DWORD),
        ]

    class Job:
        def __init__(self, cpu: int = 50, memory_mb: int = 16384, processes: int = 128) -> None:
            if os.name != "nt":
                raise RuntimeError("Only Windows process containment is qualified")
            if not 1 <= cpu <= 100 or memory_mb < 64 or not 2 <= processes <= 256:
                raise ValueError("Invalid resource limits")
            self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
            self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
            self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            self.kernel.OpenProcess.restype = wintypes.HANDLE
            self.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            self.kernel.SetInformationJobObject.argtypes = [
                wintypes.HANDLE,
                ctypes.c_int,
                ctypes.c_void_p,
                wintypes.DWORD,
            ]
            self.kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
            self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            self.kernel.QueryInformationJobObject.argtypes = [
                wintypes.HANDLE,
                ctypes.c_int,
                ctypes.c_void_p,
                wintypes.DWORD,
                ctypes.c_void_p,
            ]
            self.handle = self.kernel.CreateJobObjectW(None, None)
            if not self.handle:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                limits = ExtendedLimits()
                limits.basic.flags = 0x2000 | 0x8 | 0x200
                limits.basic.processes = processes
                limits.job_memory = memory_mb * 1024 * 1024
                if not self.kernel.SetInformationJobObject(
                    self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
                ):
                    raise ctypes.WinError(ctypes.get_last_error())
                cpu_limits = CpuLimits(0x1 | 0x4, cpu * 100)
                if not self.kernel.SetInformationJobObject(
                    self.handle, 15, ctypes.byref(cpu_limits), ctypes.sizeof(cpu_limits)
                ):
                    raise ctypes.WinError(ctypes.get_last_error())
            except BaseException:
                self.close()
                raise

        def assign(self, pid: int) -> None:
            process = self.kernel.OpenProcess(0x100 | 0x1, False, pid)
            if not process:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                if not self.kernel.AssignProcessToJobObject(self.handle, process):
                    raise ctypes.WinError(ctypes.get_last_error())
            finally:
                self.kernel.CloseHandle(process)

        def terminate(self) -> None:
            if self.handle and not self.kernel.TerminateJobObject(self.handle, 1):
                raise ctypes.WinError(ctypes.get_last_error())

        def stop_and_confirm(self, timeout: float = 5) -> None:
            self.terminate()
            deadline = time.monotonic() + timeout
            while self.handle:
                info = Accounting()
                if not self.kernel.QueryInformationJobObject(
                    self.handle, 1, ctypes.byref(info), ctypes.sizeof(info), None
                ):
                    raise ctypes.WinError(ctypes.get_last_error())
                if info.active == 0:
                    return
                if time.monotonic() >= deadline:
                    raise TimeoutError("Job members have not terminated")
                time.sleep(0.01)

        def close(self) -> None:
            if self.handle:
                self.kernel.CloseHandle(self.handle)
                self.handle = None

        def forget(self, pid: int) -> None:
            # Kernel membership ends with the process; there is no retained PID to reuse.
            pass
