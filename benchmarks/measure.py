"""Measure one Python stage in a fresh process per invocation."""

from __future__ import annotations

import argparse
import runpy
import sys
from pathlib import Path
from time import perf_counter


def peak_resident_bytes() -> int:
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class MemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        get_memory = kernel.K32GetProcessMemoryInfo
        get_memory.argtypes = [wintypes.HANDLE, ctypes.POINTER(MemoryCounters), wintypes.DWORD]
        get_memory.restype = wintypes.BOOL
        counters = MemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        if not get_memory(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            raise ctypes.WinError(ctypes.get_last_error())
        return counters.PeakWorkingSetSize

    import resource

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak if sys.platform == "darwin" else peak * 1024


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-m", "--module", action="store_true", help="run an importable module")
    parser.add_argument("target", help="script path or module name")
    parser.add_argument("args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    sys.argv = [args.target, *args.args]
    sys.path[0] = str(Path.cwd() if args.module else Path(args.target).resolve().parent)
    started = perf_counter()
    try:
        if args.module:
            runpy.run_module(args.target, run_name="__main__", alter_sys=True)
        else:
            runpy.run_path(args.target, run_name="__main__")
    finally:
        elapsed = perf_counter() - started
        print(f"wall_s={elapsed:.3f} peak_resident_mib={peak_resident_bytes() / 2**20:.2f}")


if __name__ == "__main__":
    main()
