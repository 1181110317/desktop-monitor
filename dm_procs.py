class ProcReader(threading.Thread):
    """每 2 秒采一次：进程 CPU / 内存 / 活跃 TCP 连接数（不需要管理员权限）。"""

    SNAP = 2

    def __init__(self):
        super().__init__(daemon=True)
        self.lock = threading.Lock()
        self.top = []            # [(name, cpu%, mem_bytes, conns)]
        self.count = 0
        self._stop = False
        self._prev = {}

    # ---------- Win32 结构
    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
                    ("th32DefaultHeapID", ctypes.c_void_p), ("th32ModuleID", wt.DWORD),
                    ("cntThreads", wt.DWORD), ("th32ParentProcessID", wt.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
                    ("szExeFile", wt.WCHAR * 260)]

    class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
        _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

    class MIB_TCPROW_OWNER_PID(ctypes.Structure):
        _fields_ = [("dwState", wt.DWORD), ("dwLocalAddr", wt.DWORD), ("dwLocalPort", wt.DWORD),
                    ("dwRemoteAddr", wt.DWORD), ("dwRemotePort", wt.DWORD), ("dwOwningPid", wt.DWORD)]

    def snapshot(self):
        with self.lock:
            return list(self.top), self.count

    def stop(self):
        self._stop = True

    # ---------- 采样
    def _conns_by_pid(self):
        ip = _iphlp
        ip.GetExtendedTcpTable.argtypes = [ctypes.c_void_p, ctypes.POINTER(wt.DWORD), wt.BOOL,
                                           ctypes.c_ulong, ctypes.c_int, wt.DWORD]
        size = wt.DWORD(0)
        ip.GetExtendedTcpTable(None, ctypes.byref(size), False, 2, 5, 0)
        if size.value <= 0:
            return {}
        buf = ctypes.create_string_buffer(size.value)
        if ip.GetExtendedTcpTable(buf, ctypes.byref(size), False, 2, 5, 0) != 0:
            return {}
        n = ctypes.cast(buf, ctypes.POINTER(wt.DWORD))[0]
        row_size = ctypes.sizeof(self.MIB_TCPROW_OWNER_PID)
        out = {}
        for i in range(int(n)):
            row = self.MIB_TCPROW_OWNER_PID.from_buffer(buf, 4 + i * row_size)
            if row.dwState == 2:          # LISTEN 不算
                continue
            out[row.dwOwningPid] = out.get(row.dwOwningPid, 0) + 1
        return out

    def _sample(self):
        k = _k32
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        k.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
        k.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        k.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(self.PROCESSENTRY32W)]
        k.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(self.PROCESSENTRY32W)]
        k.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
        k.OpenProcess.restype = ctypes.c_void_p
        k.GetProcessTimes.argtypes = [ctypes.c_void_p] + [ctypes.POINTER(wt.FILETIME)] * 4
        k.CloseHandle.argtypes = [ctypes.c_void_p]
        psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p,
                                               ctypes.POINTER(self.PROCESS_MEMORY_COUNTERS),
                                               wt.DWORD]

        conns = self._conns_by_pid()
        snap = k.CreateToolhelp32Snapshot(self.SNAP, 0)
        if not snap or snap == ctypes.c_void_p(-1).value:
            return
        now = time.monotonic()
        cpu_total = cores = None
        try:
            cores = os.cpu_count() or 1
        except Exception:
            cores = 1
        rows = []
        total = 0
        prev = {}
        ent = self.PROCESSENTRY32W()
        ent.dwSize = ctypes.sizeof(self.PROCESSENTRY32W)
        try:
            ok = k.Process32FirstW(snap, ctypes.byref(ent))
            while ok:
                pid = ent.th32ProcessID
                name = ent.szExeFile or ""
                total += 1
                if pid and name not in ("System Idle Process",):
                    h = k.OpenProcess(0x1000 | 0x0400, False, pid)
                    if h:
                        try:
                            c, e, kr, us = (wt.FILETIME() for _ in range(4))
                            if k.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e),
                                                 ctypes.byref(kr), ctypes.byref(us)):
                                t = ((kr.dwHighDateTime << 32) | kr.dwLowDateTime) + \
                                    ((us.dwHighDateTime << 32) | us.dwLowDateTime)
                                mc = self.PROCESS_MEMORY_COUNTERS()
                                mc.cb = ctypes.sizeof(mc)
                                mem = 0
                                if psapi.GetProcessMemoryInfo(h, ctypes.byref(mc), mc.cb):
                                    mem = mc.WorkingSetSize
                                prev[pid] = (t, mem, now, name)
                        finally:
                            k.CloseHandle(h)
                ok = k.Process32NextW(snap, ctypes.byref(ent))
        finally:
            k.CloseHandle(snap)

        old = self._prev
        self._prev = {pid: (v[0], v[1], v[2]) for pid, v in prev.items()}
        for pid, (t, mem, ts, name) in prev.items():
            o = old.get(pid)
            cpu = 0.0
            if o:
                dt = max(0.2, ts - o[2])
                cpu = 100.0 * (t - o[0]) / (dt * 1e7) / cores
            rows.append((name, max(0.0, cpu), mem, conns.get(pid, 0)))
        rows.sort(key=lambda r: (-r[1], -r[2]))
        with self.lock:
            self.top = rows[:4]
            self.count = total

    def run(self):
        while not self._stop:
            try:
                self._sample()
            except Exception:
                if os.environ.get("DM_DEBUG"):
                    import traceback
                    traceback.print_exc()
            for _ in range(int(self.SNAP * 10)):
                if self._stop:
                    return
                time.sleep(0.1)
