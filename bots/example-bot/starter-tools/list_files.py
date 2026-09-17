import os, time

def run(path="/data", depth=3, max_entries=500):
    root = os.path.abspath(path)
    if not os.path.exists(root):
        return {"root": root, "exists": False, "entries": []}
    entries = []
    base_depth = root.rstrip("/").count("/")
    for dirpath, dirnames, filenames in os.walk(root):
        d = dirpath.rstrip("/").count("/") - base_depth
        if d >= depth:
            dirnames[:] = []
        for name in sorted(dirnames) + sorted(filenames):
            full = os.path.join(dirpath, name)
            try:
                st = os.stat(full)
                entries.append({
                    "path": full,
                    "type": "dir" if os.path.isdir(full) else "file",
                    "size": st.st_size,
                    "mtime": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)),
                })
            except OSError as e:
                entries.append({"path": full, "error": str(e)})
            if len(entries) >= max_entries:
                return {"root": root, "exists": True, "truncated": True, "entries": entries}
    total = sum(e.get("size", 0) for e in entries if e.get("type") == "file")
    return {"root": root, "exists": True, "count": len(entries),
            "total_bytes": total, "entries": entries}
