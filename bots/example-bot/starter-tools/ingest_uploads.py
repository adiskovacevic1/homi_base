import os, json, sqlite3, hashlib, time, re

UP = "/data/uploads"
STATE_DIR = "/data/ingest_uploads"
STATE = os.path.join(STATE_DIR, "seen.json")
DB = "/data/rag_memory/rag.db"
TEXT_EXT = {".txt",".md",".json",".csv",".log",".py",".js",".ts",".sh",".yaml",".yml",".toml",".ini",".html",".xml",".c",".cpp",".java",".rs",".go",".sql",".conf"}

def _load():
    try:
        with open(STATE) as f: return json.load(f)
    except Exception: return {}

def _save(d):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE, "w") as f: json.dump(d, f, indent=1)

def _extract(p):
    ext = os.path.splitext(p)[1].lower()
    if ext in TEXT_EXT:
        with open(p, "rb") as f: return f.read().decode("utf-8", "replace")
    if ext == ".pdf":
        try:
            from pypdf import PdfReader
        except Exception:
            return None
        r = PdfReader(p)
        return "\n\n".join((pg.extract_text() or "") for pg in r.pages)
    return None

def _chunks(t, size=1200, overlap=150):
    t = re.sub(r"\n{3,}", "\n\n", t.strip())
    out, i = [], 0
    while i < len(t):
        out.append(t[i:i+size]); i += size - overlap
    return out or [""]

def _add_doc(title, source, tags, text):
    con = sqlite3.connect(DB)
    cur = con.cursor()
    cols = [r[1] for r in cur.execute("PRAGMA table_info(docs)")]
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    vals = {"title": title, "source": source, "tags": tags, "text": text,
            "created": now, "created_at": now, "ts": now, "body": text, "content": text}
    use = [c for c in cols if c != "id" and c in vals]
    cur.execute(f"INSERT INTO docs ({','.join(use)}) VALUES ({','.join('?'*len(use))})",
                [vals[c] for c in use])
    doc_id = cur.lastrowid
    ccols = [r[1] for r in cur.execute("PRAGMA table_info(chunks)")]
    n = 0
    for idx, ch in enumerate(_chunks(text)):
        cv = {"doc_id": doc_id, "chunk_index": idx, "idx": idx, "seq": idx,
              "text": ch, "body": ch, "content": ch, "title": title,
              "tags": tags, "source": source}
        cu = [c for c in ccols if c != "id" and c in cv]
        cur.execute(f"INSERT INTO chunks ({','.join(cu)}) VALUES ({','.join('?'*len(cu))})",
                    [cv[c] for c in cu])
        n += 1
    con.commit(); con.close()
    return doc_id, n

def run(action="scan", path=None, tags=None, force=False):
    action = action or "scan"
    seen = _load()
    if action == "status":
        files = sorted(os.listdir(UP)) if os.path.isdir(UP) else []
        return {"uploads_dir": UP, "files_present": files, "indexed": len(seen),
                "ledger": list(seen.values())[-10:]}
    if action == "forget":
        if not path: raise ValueError("path required for forget")
        removed = [k for k in list(seen) if path in k]
        for k in removed: seen.pop(k)
        _save(seen)
        return {"forgot": removed}

    target = path or UP
    if os.path.isfile(target):
        paths = [target]
    elif os.path.isdir(target):
        paths = [os.path.join(target, f) for f in sorted(os.listdir(target))]
        paths = [p for p in paths if os.path.isfile(p)]
    else:
        return {"error": f"no such path {target}", "indexed": 0}

    if not os.path.exists(DB):
        raise RuntimeError("RAG db missing at " + DB)

    done, skipped = [], []
    for p in paths:
        try:
            h = hashlib.sha256(open(p, "rb").read()).hexdigest()[:16]
        except Exception as e:
            skipped.append({"file": p, "why": str(e)}); continue
        key = f"{p}:{h}"
        if key in seen and not force:
            skipped.append({"file": os.path.basename(p), "why": "already indexed"}); continue
        text = _extract(p)
        if not text or not text.strip():
            skipped.append({"file": os.path.basename(p), "why": "unsupported or empty"}); continue
        t = ",".join(filter(None, ["upload", tags]))
        doc_id, n = _add_doc(os.path.basename(p), p, t, text)
        seen[key] = {"file": os.path.basename(p), "doc_id": doc_id, "chunks": n,
                     "at": time.strftime("%Y-%m-%d %H:%M")}
        done.append(seen[key])
    _save(seen)
    return {"newly_indexed": done, "skipped": skipped, "total_tracked": len(seen)}
