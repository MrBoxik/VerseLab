"""Dedicated-memory admission for local transcription on Windows."""
import ctypes as ct
from ctypes import wintypes as win
import os
from uuid import UUID

MAX_AGENTS = 4
AGENT_BYTES = 5 * 1024**3
HEADROOM_BYTES = 768 * 1024**2


class _Luid(ct.Structure):
    _fields_ = [("low", win.DWORD), ("high", win.LONG)]


class _AdapterDescription(ct.Structure):
    _fields_ = [("description", win.WCHAR * 128), ("ids", win.UINT * 4),
                ("dedicated", ct.c_size_t), ("system", ct.c_size_t), ("shared", ct.c_size_t),
                ("luid", _Luid), ("flags", win.UINT)]


class _CounterValue(ct.Structure):
    _fields_ = [("status", win.DWORD), ("value", ct.c_longlong)]


def _method(instance, slot, result, *arguments):
    table = ct.cast(instance, ct.POINTER(ct.POINTER(ct.c_void_p)))[0]
    return ct.WINFUNCTYPE(result, ct.c_void_p, *arguments)(table[slot])


def _dedicated_adapters():
    # IDXGIFactory1/IDXGIAdapter1 vtable slots follow dxgi.h.
    iid = UUID("770aae78-f26f-4dba-a829-253c83d1b387").bytes_le
    factory = ct.c_void_p()
    if ct.windll.dxgi.CreateDXGIFactory1(ct.c_char_p(iid), ct.byref(factory)) != 0:
        return []
    adapters = []
    try:
        enum = _method(factory, 12, win.LONG, win.UINT, ct.POINTER(ct.c_void_p))
        for index in range(32):
            adapter = ct.c_void_p()
            if enum(factory, index, ct.byref(adapter)) != 0:
                break
            try:
                description = _AdapterDescription()
                get_desc = _method(adapter, 10, win.LONG, ct.POINTER(_AdapterDescription))
                if get_desc(adapter, ct.byref(description)) == 0 and not description.flags & 2 and description.dedicated:
                    adapters.append(description)
            finally:
                _method(adapter, 2, win.ULONG)(adapter)
    finally:
        _method(factory, 2, win.ULONG)(factory)
    return adapters


def _memory_counters(adapter, worker_pids):
    query = ct.c_void_p()
    pdh = ct.windll.pdh
    if pdh.PdhOpenQueryW(None, None, ct.byref(query)) != 0:
        return None
    instance = f"luid_0x{adapter.luid.high & 0xffffffff:08x}_0x{adapter.luid.low:08x}_phys_0"
    handles = []
    try:
        paths = [rf"\GPU Adapter Memory({instance})\Dedicated Usage"]
        paths.extend(rf"\GPU Process Memory(pid_{pid}_{instance})\Dedicated Usage" for pid in worker_pids)
        for path in paths:
            counter = ct.c_void_p()
            status = pdh.PdhAddEnglishCounterW(query, ct.c_wchar_p(path), None, ct.byref(counter))
            handles.append(counter if status == 0 else None)
        if handles[0] is None or pdh.PdhCollectQueryData(query) != 0:
            return None
        values = []
        for counter in handles:
            data = _CounterValue()
            valid = (counter and pdh.PdhGetFormattedCounterValue(counter, 0x400, None, ct.byref(data)) == 0
                     and data.status in (0, 1) and data.value >= 0)
            values.append(data.value if valid else None)
        if values[0] is None:
            return None
        return values[0], sum(value or 0 for value in values[1:])
    finally:
        pdh.PdhCloseQuery(query)


def available_agents(requested, worker_pids):
    if requested <= 1 or os.name != "nt":
        return 1
    try:
        adapters = _dedicated_adapters()
        # One physical GPU may appear under more than one WDDM LUID.
        if len({tuple(adapter.ids) for adapter in adapters}) == 1:
            free = []
            for adapter in adapters:
                counters = _memory_counters(adapter, worker_pids)
                if counters is not None:
                    used, ours = counters
                    free.append(adapter.dedicated - max(0, used - ours) - HEADROOM_BYTES)
            if free:
                return max(1, min(requested, min(free) // AGENT_BYTES))
    except (AttributeError, OSError, ValueError):
        pass
    return 1
