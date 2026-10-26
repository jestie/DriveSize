#!/usr/bin/env python3
"""DriveSize: a TreeSize-style disk usage explorer.

Standard library only, so it runs under Windows Python (fast native scans of
C:, D: ...) or Linux/WSL Python. Serves a local web UI on 127.0.0.1.

    python drivesize.py [--port 8765] [--no-browser]
"""
import argparse
import heapq
import json
import os
import secrets
import shutil
import socket
import stat
import sys
import threading
import time
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

IS_WIN = os.name == "nt"
HERE = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------- categories

CATEGORIES = ["Video", "Images", "Audio", "Documents", "Archives", "Code",
              "Programs", "Disk images", "System & data", "Temp & logs", "Other"]
_EXT = {}
for _cat, _exts in {
    "Video": "mp4 mkv avi mov wmv flv webm m4v mpg mpeg ts m2ts vob 3gp",
    "Images": "jpg jpeg png gif bmp tif tiff webp heic heif raw cr2 cr3 nef arw dng psd svg ico",
    "Audio": "mp3 wav flac aac ogg m4a wma opus aiff mid",
    "Documents": "pdf doc docx xls xlsx ppt pptx odt ods odp txt rtf md csv epub mobi pst ost one vsdx",
    "Archives": "zip rar 7z tar gz tgz bz2 xz zst cab lz4 jar war",
    "Code": "py js ts jsx tsx java c h cpp hpp cs go rs rb php html css scss json xml yml yaml sh ps1 bat sql ipynb lock map",
    "Programs": "exe dll msi msix appx sys so dylib bin apk deb rpm snap pyd node",
    "Disk images": "iso img vhd vhdx vmdk vdi qcow2 ova ovf wim esd dmg",
    "System & data": "db sqlite sqlite3 mdf ldf dat pak cache idx pack blob ldb sst edb jrs",
    "Temp & logs": "tmp temp log dmp bak old etl crdownload part partial",
}.items():
    for _e in _exts.split():
        _EXT[_e] = CATEGORIES.index(_cat)
OTHER = CATEGORIES.index("Other")
NCAT = len(CATEGORIES)


def ext_of(name):
    i = name.rfind(".")
    return name[i + 1:].lower() if 0 < i < len(name) - 1 else ""


# ---------------------------------------------------------- safety heuristics
# Levels: "safe" (temp/cache, regenerates on its own), "review" (probably
# unneeded but check first), "system" (never delete by hand).

SAFE_DIRS = {
    "temp", "tmp", "cache", "caches", ".cache", "cachedata", "code cache",
    "gpucache", "shadercache", "dxcache", "d3dscache", "inetcache",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "crashdumps",
    "crashpad", "_cacache", "service worker", "cachestorage", "webcache",
    "reportarchive", "reportqueue", ".parcel-cache", ".turbo", "thumbnails",
}
REVIEW_DIRS = {
    "node_modules": "Node packages; restored with npm install",
    ".venv": "Python virtual env; can be recreated",
    "venv": "Python virtual env; can be recreated",
    "windows.old": "Previous Windows install; remove via Disk Cleanup",
    ".gradle": "Gradle cache/build data; re-downloads on demand",
    ".nuget": "NuGet package cache; re-downloads on demand",
    ".m2": "Maven repository cache; re-downloads on demand",
    ".next": "Next.js build output; rebuilt on demand",
    "dist": "Build output; usually rebuilt from source",
    "target": "Build output (Rust/Java); rebuilt from source",
    "$windows.~bt": "Leftover Windows upgrade files",
    "$windows.~ws": "Leftover Windows upgrade files",
    "downloads": "Downloads folder; often full of old installers",
}
SYSTEM_NAMES = {
    "windows", "program files", "program files (x86)", "programdata",
    "system volume information", "$recycle.bin", "recovery", "boot",
    "pagefile.sys", "hiberfil.sys", "swapfile.sys", "dumpstack.log.tmp",
    "ext4.vhdx", "$extend", "winsxs", "msocache",
}
SAFE_EXT = {"tmp", "temp", "dmp", "crdownload", "part", "partial", "etl"}
REVIEW_EXT = {"log": "Log file", "bak": "Backup copy", "old": "Old copy",
              "iso": "Disk image; often only needed once",
              "msi": "Installer; usually not needed after install",
              "zip": "Archive; check it was extracted", "7z": "Archive",
              "rar": "Archive"}
OLD_SECS = 2 * 365 * 86400
# Only personal-data kinds count as "old and forgotten"; app binaries don't.
OLD_CATS = {CATEGORIES.index(c) for c in
            ("Video", "Images", "Audio", "Documents", "Archives", "Disk images")}


def classify_dir(name, path_lower):
    n = name.lower()
    if n in SYSTEM_NAMES:
        return "system", "System folder; do not delete by hand"
    if "softwaredistribution" in path_lower and n == "download":
        return "safe", "Windows Update download cache"
    if n in SAFE_DIRS:
        return "safe", "Temporary/cache data; regenerates automatically"
    if n in REVIEW_DIRS:
        return "review", REVIEW_DIRS[n]
    return None


def classify_file(name, size, mtime, now):
    n = name.lower()
    if n in SYSTEM_NAMES:
        return "system", "System file; managed by the OS"
    if n.startswith("thumbcache_") or n.startswith("~$"):
        return "safe", "Thumbnail cache / Office lock file"
    e = ext_of(n)
    if e in SAFE_EXT:
        return "safe", "Temporary file / crash dump"
    if e in REVIEW_EXT:
        return "review", REVIEW_EXT[e]
    if size >= 100 * 2**20 and mtime and now - mtime > OLD_SECS and _EXT.get(e) in OLD_CATS:
        return "review", "Large and untouched for 2+ years"
    return None


# --------------------------------------------------------------------- drives

def list_drives():
    drives = []
    if IS_WIN:
        import ctypes
        k32 = ctypes.windll.kernel32
        mask = k32.GetLogicalDrives()
        kinds = {2: "Removable", 3: "Local disk", 4: "Network", 5: "CD/DVD", 6: "RAM disk"}
        for i in range(26):
            if not mask & (1 << i):
                continue
            root = chr(65 + i) + ":\\"
            kind = kinds.get(k32.GetDriveTypeW(root), "Drive")
            label = ctypes.create_unicode_buffer(261)
            fs = ctypes.create_unicode_buffer(261)
            ok = k32.GetVolumeInformationW(root, label, 261, None, None, None, fs, 261)
            try:
                u = shutil.disk_usage(root)
            except OSError:
                continue
            drives.append({"path": root, "label": label.value if ok else "",
                           "fs": fs.value if ok else "", "kind": kind,
                           "total": u.total, "used": u.used, "free": u.free})
        return drives
    pseudo = {"proc", "sysfs", "tmpfs", "devtmpfs", "devpts", "cgroup", "cgroup2",
              "overlay", "squashfs", "fuse.snapfuse", "mqueue", "debugfs",
              "tracefs", "securityfs", "pstore", "bpf", "autofs", "binfmt_misc",
              "configfs", "hugetlbfs", "fusectl", "rootfs", "nsfs", "ramfs"}
    seen = set()
    with open("/proc/mounts") as f:
        for line in f:
            dev, mnt, fs = line.split()[:3]
            mnt = mnt.replace("\\040", " ")
            if fs in pseudo or mnt in seen:
                continue
            if not (mnt == "/" or mnt.startswith(("/mnt/", "/media/", "/home", "/data"))):
                continue
            if mnt.startswith("/mnt/wsl") or mnt.startswith("/usr/lib/wsl"):
                continue
            try:
                u = shutil.disk_usage(mnt)
            except OSError:
                continue
            seen.add(mnt)
            label = dev.rstrip("\\") if fs in ("9p", "drvfs") else os.path.basename(mnt) or "Linux root"
            drives.append({"path": mnt, "label": label, "fs": fs,
                           "kind": "Windows drive (slow via WSL)" if fs in ("9p", "drvfs") else "Local disk",
                           "total": u.total, "used": u.used, "free": u.free})
    return drives


# ---------------------------------------------------------------------- scan

class Node:
    __slots__ = ("name", "parent", "children", "size", "files", "dirs",
                 "cats", "mtime", "flag", "id", "done")

    def __init__(self, name, parent, nid):
        self.name = name
        self.parent = parent
        self.children = []
        self.size = 0          # total bytes, including subfolders
        self.files = 0         # total file count, including subfolders
        self.dirs = 0          # total subfolder count
        self.cats = [0] * NCAT
        self.mtime = 0
        self.flag = None
        self.id = nid
        self.done = False


class Scan:
    def __init__(self, root, workers):
        self.root_path = os.path.abspath(root)
        self.nodes = []
        self.lock = threading.Lock()
        self.root = self._new(self.root_path, None)
        self.state = "running"
        self.started = time.time()
        self.finished = None
        self.errors = 0
        self.current = self.root_path
        self.cancel = False
        self.ext = {}               # ext -> [bytes, count]
        self.big = []               # min-heap of (size, path, mtime)
        self.flagged_files = []     # (size, path, level, reason, mtime)
        self.dupe_pool = {}         # (size, name) -> [paths]
        self.cleanup = []
        self.dupes = []
        try:
            self.dev = os.stat(self.root_path).st_dev
        except OSError:
            self.dev = None
        self.workers = workers
        threading.Thread(target=self._run, daemon=True).start()

    def _new(self, name, parent):
        with self.lock:
            n = Node(name, parent, len(self.nodes))
            self.nodes.append(n)
        return n

    def path_of(self, node):
        parts = []
        while node.parent is not None:
            parts.append(node.name)
            node = node.parent
        parts.append(node.name)
        return os.path.join(*reversed(parts))

    # -- walk

    def _run(self):
        q = deque([self.root])
        cv = threading.Condition()
        busy = [0]

        def worker():
            while True:
                with cv:
                    while not q and busy[0] and not self.cancel:
                        cv.wait(0.2)
                    if self.cancel or (not q and not busy[0]):
                        cv.notify_all()
                        return
                    node = q.popleft()
                    busy[0] += 1
                subs = []
                try:
                    subs = self._scan_dir(node)
                finally:
                    with cv:
                        q.extend(subs)
                        busy[0] -= 1
                        cv.notify_all()

        threads = [threading.Thread(target=worker, daemon=True) for _ in range(self.workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self._finalize()
        self.finished = time.time()
        self.state = "cancelled" if self.cancel else "done"

    def _scan_dir(self, node):
        path = self.path_of(node)
        self.current = path
        now = time.time()
        own_size = own_files = 0
        cats = [0] * NCAT
        subs = []
        local_ext = {}
        prot = protected(path + os.sep + "x") is not None   # inside a system area?
        try:
            it = os.scandir(path)
        except OSError:
            self.errors += 1
            node.done = True
            return subs
        with it:
            for e in it:
                try:
                    if e.is_symlink():
                        continue
                    if e.is_dir(follow_symlinks=False):
                        if IS_WIN:
                            if getattr(e, "is_junction", lambda: False)():
                                continue
                            st = e.stat(follow_symlinks=False)
                            if st.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                                continue
                        elif self.dev is not None:
                            st = e.stat(follow_symlinks=False)
                            if st.st_dev != self.dev:
                                continue   # don't cross into other mounts
                        child = self._new(e.name, node)
                        try:
                            child.mtime = e.stat(follow_symlinks=False).st_mtime
                        except OSError:
                            pass
                        if not prot:
                            child.flag = classify_dir(e.name, (path + os.sep + e.name).lower())
                        node.children.append(child)
                        subs.append(child)
                        continue
                    st = e.stat(follow_symlinks=False)
                    if not stat.S_ISREG(st.st_mode):
                        continue
                    sz = st.st_size
                    own_size += sz
                    own_files += 1
                    x = ext_of(e.name)
                    cats[_EXT.get(x, OTHER)] += sz
                    le = local_ext.get(x)
                    if le:
                        le[0] += sz
                        le[1] += 1
                    else:
                        local_ext[x] = [sz, 1]
                    if sz >= 1 << 20:
                        self._track_file(path, e.name, sz, st.st_mtime, now, prot)
                except OSError:
                    self.errors += 1
        with self.lock:
            for x, (sz, c) in local_ext.items():
                g = self.ext.get(x)
                if g:
                    g[0] += sz
                    g[1] += c
                else:
                    self.ext[x] = [sz, c]
            # Propagate up the chain so browsing works while the scan runs.
            n = node
            while n is not None:
                n.size += own_size
                n.files += own_files
                n.dirs += len(subs)
                nc = n.cats
                for i in range(NCAT):
                    if cats[i]:
                        nc[i] += cats[i]
                n = n.parent
        node.done = True
        return subs

    def _track_file(self, dirpath, name, size, mtime, now, prot):
        full = os.path.join(dirpath, name)
        with self.lock:
            item = (size, full, mtime)
            if len(self.big) < 1000:
                heapq.heappush(self.big, item)
            elif size > self.big[0][0]:
                heapq.heapreplace(self.big, item)
            if size >= 10 << 20:
                key = (size, name.lower())
                self.dupe_pool.setdefault(key, []).append(full)
        f = None if prot else classify_file(name, size, mtime, now)
        if f and f[0] != "system":
            with self.lock:
                self.flagged_files.append((size, full, f[0], f[1], mtime))

    def _finalize(self):
        out = []
        stack = [(c, False) for c in self.root.children]
        while stack:
            c, flagged_above = stack.pop()
            if c.flag and c.flag[0] != "system" and not flagged_above and c.size > 0:
                out.append({"id": c.id, "path": self.path_of(c), "size": c.size,
                            "files": c.files, "level": c.flag[0], "reason": c.flag[1],
                            "kind": "dir", "mtime": c.mtime})
                flagged_above = True
            stack.extend((k, flagged_above) for k in c.children)
        dir_prefixes = tuple(r["path"] + os.sep for r in out)
        for size, path, level, reason, mtime in self.flagged_files:
            if dir_prefixes and path.startswith(dir_prefixes):
                continue   # already covered by a flagged folder
            out.append({"path": path, "size": size, "files": 1, "level": level,
                        "reason": reason, "kind": "file", "mtime": mtime})
        out.sort(key=lambda r: -r["size"])
        self.cleanup = out[:2000]
        groups = []
        for (size, name), paths in self.dupe_pool.items():
            if len(paths) > 1:
                groups.append({"size": size, "name": name, "paths": sorted(paths),
                               "waste": size * (len(paths) - 1)})
        groups.sort(key=lambda g: -g["waste"])
        self.dupes = groups[:500]

    # -- removal bookkeeping

    def find_node(self, path):
        rel = os.path.relpath(path, self.root_path)
        if rel.startswith(".."):
            return None
        node = self.root
        if rel == ".":
            return node
        for part in rel.split(os.sep):
            node = next((c for c in node.children if c.name == part), None)
            if node is None:
                return None
        return node

    def forget(self, path, is_dir, size):
        """Subtract a deleted item from the tree and side lists."""
        with self.lock:
            if is_dir:
                node = self.find_node(path)
                if node is None or node.parent is None:
                    return
                parent = node.parent
                parent.children = [c for c in parent.children if c is not node]
                size, files, dirs, cats = node.size, node.files, node.dirs + 1, node.cats
            else:
                parent = self.find_node(os.path.dirname(path))
                if parent is None:
                    return
                files, dirs = 1, 0
                cats = [0] * NCAT
                cats[_EXT.get(ext_of(path), OTHER)] = size
            n = parent
            while n is not None:
                n.size -= size
                n.files -= files
                n.dirs -= dirs
                for i in range(NCAT):
                    n.cats[i] -= cats[i]
                n = n.parent
            pre = path.rstrip(os.sep) + os.sep
            gone = lambda p: p == path or p.startswith(pre)
            self.cleanup = [r for r in self.cleanup if not gone(r["path"])]
            self.big = [b for b in self.big if not gone(b[1])]
            heapq.heapify(self.big)
            for g in self.dupes:
                g["paths"] = [p for p in g["paths"] if not gone(p)]
                g["waste"] = g["size"] * max(0, len(g["paths"]) - 1)
            self.dupes = [g for g in self.dupes if len(g["paths"]) > 1]

    def shrink(self, path, size, files, cats):
        """A folder was only partly deleted (files in use): subtract what went."""
        with self.lock:
            n = self.find_node(path)
            while n is not None:
                n.size -= size
                n.files -= files
                for i in range(NCAT):
                    n.cats[i] -= cats[i]
                n = n.parent


SCANS = {}
SCAN_LOCK = threading.Lock()


def norm(p):
    p = os.path.abspath(p)
    return p.lower() if IS_WIN else p


def scan_for(path):
    """Return the scan whose root contains path (deepest root wins)."""
    np = norm(path)
    best = None
    for root, s in SCANS.items():
        if np == root or np.startswith(root.rstrip(os.sep) + os.sep):
            if best is None or len(root) > len(norm(best.root_path)):
                best = s
    return best


# ------------------------------------------------------------------- delete

def protected(path):
    p = os.path.abspath(path)
    pl = p.lower() if IS_WIN else p
    if os.path.dirname(p) == p:
        return "Refusing to delete a drive root"
    if IS_WIN:
        drive = os.path.splitdrive(pl)[0] + "\\"
        rest = pl[len(drive):]
        top = rest.split("\\")[0]
        if top in ("windows", "program files", "program files (x86)", "programdata",
                   "system volume information", "$recycle.bin", "recovery", "boot"):
            ok = rest.startswith("windows\\temp\\") or \
                rest.startswith("windows\\softwaredistribution\\download\\")
            if not ok:
                return "Protected system location"
        if rest in ("users", "pagefile.sys", "hiberfil.sys", "swapfile.sys") or \
                (rest.startswith("users\\") and rest.count("\\") <= 1):
            return "Protected system location"
        if os.path.basename(pl) == "ext4.vhdx":
            return "This is a WSL virtual disk"
    else:
        sysdirs = ("/bin", "/boot", "/dev", "/etc", "/lib", "/lib32", "/lib64",
                   "/libx32", "/proc", "/run", "/sbin", "/snap", "/sys", "/usr",
                   "/var/lib", "/opt", "/root", "/srv")
        if any(p == d or p.startswith(d + "/") for d in sysdirs):
            return "Protected system location"
        home = os.path.expanduser("~")
        if p in ("/home", "/mnt", "/media", "/var", home):
            return "Protected location"
        if p.startswith("/mnt/") and p.count("/") == 2:
            return "Refusing to delete a drive root"
    return None


def recycle_windows(path):
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT),
                    ("pFrom", wintypes.LPCWSTR), ("pTo", wintypes.LPCWSTR),
                    ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]

    FO_DELETE, FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI = 3, 4, 0x10, 0x40, 0x400
    op = SHFILEOPSTRUCTW(None, FO_DELETE, path + "\0", None,
                         FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI,
                         False, None, None)
    rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if rc or op.fAnyOperationsAborted:
        raise OSError("Recycle Bin move failed (code %d)" % rc)


def trash_linux(path):
    from urllib.parse import quote
    base = os.path.join(os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")), "Trash")
    os.makedirs(os.path.join(base, "files"), exist_ok=True)
    os.makedirs(os.path.join(base, "info"), exist_ok=True)
    if os.stat(path, follow_symlinks=False).st_dev != os.stat(base).st_dev:
        raise OSError("Trash is on a different drive; use permanent delete for this item")
    name = os.path.basename(path)
    target, i = name, 1
    while os.path.exists(os.path.join(base, "files", target)):
        i += 1
        target = "%s.%d" % (name, i)
    with open(os.path.join(base, "info", target + ".trashinfo"), "w") as f:
        f.write("[Trash Info]\nPath=%s\nDeletionDate=%s\n"
                % (quote(path), time.strftime("%Y-%m-%dT%H:%M:%S")))
    os.rename(path, os.path.join(base, "files", target))


def _is_link(e):
    if e.is_symlink():
        return True
    if IS_WIN and e.is_dir(follow_symlinks=False):
        if getattr(e, "is_junction", lambda: False)():
            return True
        return bool(e.stat(follow_symlinks=False).st_file_attributes
                    & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    return False


def _remove_file(p):
    try:
        os.remove(p)
    except PermissionError:
        if not IS_WIN:
            raise
        os.chmod(p, stat.S_IWRITE)   # read-only files refuse deletion on Windows
        os.remove(p)


class DeleteJob:
    """Deletes items in a background thread so the UI can show progress.

    Permanent folder deletes go file by file: files in use are skipped and
    counted instead of aborting the whole folder like shutil.rmtree would.
    """

    def __init__(self, items, permanent):
        self.id = secrets.token_hex(6)
        self.items = items              # [{"path", "size"}]
        self.permanent = permanent
        self.total = sum(max(0, int(i.get("size") or 0)) for i in items)
        self.state = "running"
        self.index = 0
        self.current = ""
        self.done_bytes = 0             # bytes accounted for (progress bar)
        self.freed = 0
        self.files = 0
        self.skipped = 0
        self.results = []
        self.cancel = False
        self.started = time.time()
        self.finished = None
        threading.Thread(target=self._run, daemon=True).start()

    def status(self):
        return {"id": self.id, "state": self.state, "index": self.index,
                "count": len(self.items), "current": self.current,
                "total": self.total, "done": self.done_bytes, "freed": self.freed,
                "files": self.files, "skipped": self.skipped,
                "permanent": self.permanent, "results": self.results,
                "elapsed": (self.finished or time.time()) - self.started}

    def _run(self):
        for i, item in enumerate(self.items):
            if self.cancel:
                break
            self.index = i + 1
            path = os.path.abspath(item["path"])
            self.current = path
            base = self.done_bytes
            try:
                self._one(path)
                self.results.append({"path": item["path"], "ok": True})
            except Exception as e:  # noqa: BLE001 - reported per item
                self.results.append({"path": item["path"], "ok": False, "error": str(e)})
            self.done_bytes = max(self.done_bytes, base + max(0, int(item.get("size") or 0)))
        self.finished = time.time()
        self.state = "cancelled" if self.cancel else "done"

    def _one(self, path):
        why = protected(path)
        if why:
            raise PermissionError(why)
        if not os.path.lexists(path):
            return                       # already gone (e.g. inside an earlier item)
        is_dir = os.path.isdir(path) and not os.path.islink(path)
        size = 0 if is_dir else os.path.getsize(path)
        scan = scan_for(path)
        if not self.permanent:
            recycle_windows(path) if IS_WIN else trash_linux(path)
            self.freed += size if not is_dir else (scan_node_size(scan, path) or 0)
        elif not is_dir:
            _remove_file(path)
            self.freed += size
            self.files += 1
            self.done_bytes += size
        else:
            freed, files, cats = self._purge(path)
            if os.path.exists(path):
                if scan:
                    scan.shrink(path, freed, files, cats)
                if self.cancel:
                    return
                raise OSError("Partly deleted: %d files were in use or locked and were left"
                              % self._item_skipped)
        if scan:
            scan.forget(path, is_dir, size)

    def _purge(self, root):
        freed = files = 0
        cats = [0] * NCAT
        skipped_before = self.skipped
        stack = [(root, False)]
        while stack and not self.cancel:
            d, visited = stack.pop()
            if visited:
                try:
                    os.rmdir(d)
                except OSError:
                    pass                 # still holds skipped files
                continue
            stack.append((d, True))
            self.current = d
            try:
                it = os.scandir(d)
            except OSError:
                self.skipped += 1
                continue
            with it:
                for e in it:
                    if self.cancel:
                        break
                    try:
                        if _is_link(e):
                            try:
                                os.unlink(e.path)
                            except OSError:
                                os.rmdir(e.path)
                            continue
                        if e.is_dir(follow_symlinks=False):
                            stack.append((e.path, False))
                            continue
                        sz = e.stat(follow_symlinks=False).st_size
                        _remove_file(e.path)
                        freed += sz
                        files += 1
                        cats[_EXT.get(ext_of(e.name), OTHER)] += sz
                        self.freed += sz
                        self.files += 1
                        self.done_bytes += sz
                    except OSError:
                        self.skipped += 1
        self._item_skipped = self.skipped - skipped_before
        return freed, files, cats


def scan_node_size(scan, path):
    if scan is None:
        return None
    with scan.lock:
        n = scan.find_node(path)
        return n.size if n else None


JOBS = {}


# ---------------------------------------------------------------------- API

def node_json(s, n):
    return {"id": n.id, "name": n.name, "size": n.size, "files": n.files,
            "dirs": n.dirs, "mtime": n.mtime, "done": n.done,
            "flag": n.flag}


def api_node(s, nid):
    n = s.nodes[nid]
    path = s.path_of(n)
    crumbs = []
    c = n
    while c is not None:
        crumbs.append({"id": c.id, "name": c.name})
        c = c.parent
    crumbs.reverse()
    with s.lock:
        kids = sorted(n.children, key=lambda c: -c.size)
        children = [node_json(s, c) for c in kids]
        cats = list(n.cats)
        info = node_json(s, n)
    files = []
    now = time.time()
    try:
        with os.scandir(path) as it:
            for e in it:
                try:
                    if e.is_symlink() or e.is_dir(follow_symlinks=False):
                        continue
                    st = e.stat(follow_symlinks=False)
                    files.append({"name": e.name, "size": st.st_size, "mtime": st.st_mtime,
                                  "cat": CATEGORIES[_EXT.get(ext_of(e.name), OTHER)],
                                  "flag": classify_file(e.name, st.st_size, st.st_mtime, now)})
                except OSError:
                    pass
    except OSError:
        pass
    files.sort(key=lambda f: -f["size"])
    return {"node": info, "path": path, "crumbs": crumbs, "children": children,
            "files": files[:3000], "fileTotal": len(files),
            "cats": [{"name": CATEGORIES[i], "size": cats[i]} for i in range(NCAT) if cats[i] > 0],
            "sep": os.sep}


def api_status(s):
    return {"root": s.root_path, "key": norm(s.root_path), "rootId": s.root.id, "state": s.state,
            "files": s.root.files, "dirs": s.root.dirs, "bytes": s.root.size,
            "errors": s.errors, "current": s.current,
            "elapsed": (s.finished or time.time()) - s.started}


def api_types(s):
    with s.lock:
        items = sorted(s.ext.items(), key=lambda kv: -kv[1][0])
    return {"exts": [{"ext": k or "(none)", "size": v[0], "count": v[1],
                      "cat": CATEGORIES[_EXT.get(k, OTHER)]} for k, v in items[:200]],
            "cats": [{"name": CATEGORIES[i], "size": s.root.cats[i]}
                     for i in range(NCAT) if s.root.cats[i] > 0]}


def api_big(s):
    with s.lock:
        items = sorted(s.big, reverse=True)
    now = time.time()
    return {"files": [{"path": p, "size": sz, "mtime": mt,
                       "cat": CATEGORIES[_EXT.get(ext_of(p), OTHER)],
                       "flag": classify_file(os.path.basename(p), sz, mt, now)}
                      for sz, p, mt in items]}


class Handler(BaseHTTPRequestHandler):
    token = ""

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _authed(self):
        # Host check blocks DNS-rebinding pages from reading the token.
        host = self.headers.get("Host", "").rsplit(":", 1)[0]
        if host not in ("127.0.0.1", "localhost"):
            return False
        return secrets.compare_digest(self.headers.get("X-Token", ""), self.token)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/":
            if self.headers.get("Host", "").rsplit(":", 1)[0] not in ("127.0.0.1", "localhost"):
                return self._send(403, {"error": "bad host"})
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                html = f.read().replace(b"__TOKEN__", self.token.encode())
            return self._send(200, html, "text/html; charset=utf-8")
        if not self._authed():
            return self._send(403, {"error": "bad token"})
        try:
            if u.path == "/api/job":
                job = JOBS.get(q.get("id", ""))
                if job is None:
                    return self._send(404, {"error": "no such job"})
                return self._send(200, job.status())
            if u.path == "/api/drives":
                return self._send(200, {"drives": list_drives(), "windows": IS_WIN,
                                        "scans": [api_status(s) for s in SCANS.values()]})
            s = SCANS.get(q.get("root", ""))
            if s is None:
                return self._send(404, {"error": "no such scan"})
            if u.path == "/api/status":
                return self._send(200, api_status(s))
            if u.path == "/api/node":
                return self._send(200, api_node(s, int(q.get("id", 0))))
            if u.path == "/api/types":
                return self._send(200, api_types(s))
            if u.path == "/api/big":
                return self._send(200, api_big(s))
            if u.path == "/api/cleanup":
                return self._send(200, {"items": s.cleanup, "done": s.state != "running"})
            if u.path == "/api/dupes":
                return self._send(200, {"groups": s.dupes, "done": s.state != "running"})
            self._send(404, {"error": "not found"})
        except Exception as e:  # noqa: BLE001 - surface to UI
            self._send(500, {"error": str(e)})

    def do_POST(self):
        if not self._authed():
            return self._send(403, {"error": "bad token"})
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        u = urlparse(self.path)
        if u.path == "/api/scan":
            path = body.get("path", "")
            if not os.path.isdir(path):
                return self._send(400, {"error": "Not a folder: " + path})
            key = norm(path)
            with SCAN_LOCK:
                old = SCANS.get(key)
                if old and old.state == "running":
                    return self._send(200, {"root": key})
                if old:
                    old.nodes.clear()
                workers = 4 if IS_WIN else 16
                SCANS[key] = Scan(path, workers)
            return self._send(200, {"root": key})
        if u.path == "/api/cancel":
            s = SCANS.get(body.get("root", ""))
            if s:
                s.cancel = True
            return self._send(200, {"ok": True})
        if u.path == "/api/delete":
            items = [i for i in body.get("items", []) if isinstance(i, dict) and i.get("path")]
            job = DeleteJob(items, bool(body.get("permanent")))
            JOBS[job.id] = job
            return self._send(200, {"job": job.id})
        if u.path == "/api/job/cancel":
            job = JOBS.get(body.get("id", ""))
            if job:
                job.cancel = True
            return self._send(200, {"ok": True})
        if u.path == "/api/reveal":
            p = body.get("path", "")
            if IS_WIN and os.path.exists(p):
                import subprocess
                subprocess.Popen(["explorer", "/select,", os.path.abspath(p)])
            return self._send(200, {"ok": True})
        self._send(404, {"error": "not found"})


def free_port(preferred):
    for port in [preferred] + list(range(preferred + 1, preferred + 50)):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    Handler.token = secrets.token_urlsafe(24)
    port = free_port(a.port)
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = "http://127.0.0.1:%d/" % port
    print("DriveSize running at %s  (Ctrl+C to stop)" % url, flush=True)
    if not a.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
