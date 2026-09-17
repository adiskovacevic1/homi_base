import os, re, sqlite3, time, json

DIR = "/data/rag_memory"
DB = os.path.join(DIR, "rag.db")


def _conn():
    os.makedirs(DIR, exist_ok=True)
    c = sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS docs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT, source TEXT, tags TEXT, text TEXT, created REAL)""")
    c.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(
        body, title, tags, doc_id UNINDEXED, idx UNINDEXED)""")
    return c


def _chunk(text, size=1200, overlap=150):
    text = re.sub(r"\n{3,}", "\n\n", text.strip())
    if len(text) <= size:
        return [text] if text else []
    parts, i = [], 0
    while i < len(text):
        end = min(len(text), i + size)
        if end < len(text):
            cut = text.rfind("\n", i + size // 2, end)
            if cut == -1:
                cut = text.rfind(". ", i + size // 2, end)
            if cut != -1:
                end = cut + 1
        parts.append(text[i:end].strip())
        if end >= len(text):
            break
        i = max(end - overlap, i + 1)
    return [p for p in parts if p]


def _add(c, text, title, source, tags):
    if not text or not text.strip():
        raise ValueError("add needs non-empty 'text'")
    cur = c.execute("INSERT INTO docs(title,source,tags,text,created) VALUES(?,?,?,?,?)",
                    (title or "", source or "", tags or "", text, time.time()))
    did = cur.lastrowid
    ch = _chunk(text)
    for i, p in enumerate(ch):
        c.execute("INSERT INTO chunks(body,title,tags,doc_id,idx) VALUES(?,?,?,?,?)",
                  (p, title or "", tags or "", did, i))
    c.commit()
    return {"doc_id": did, "chunks": len(ch), "chars": len(text), "title": title or ""}


def run(**kw):
    action = kw.get("action") or "search"
    limit = int(kw.get("limit") or 8)
    c = _conn()

    if action == "add":
        return _add(c, kw.get("text"), kw.get("title"), kw.get("source"), kw.get("tags"))

    if action == "ingest_url":
        url = kw.get("url")
        if not url:
            raise ValueError("ingest_url needs 'url'")
        import httpx, html
        r = httpx.get(url, follow_redirects=True, timeout=25,
                      headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        body = r.text
        body = re.sub(r"(?is)<(script|style|nav|footer)[^>]*>.*?</\1>", " ", body)
        title = kw.get("title") or ""
        m = re.search(r"(?is)<title[^>]*>(.*?)</title>", body)
        if m and not title:
            title = html.unescape(m.group(1)).strip()[:200]
        txt = html.unescape(re.sub(r"(?s)<[^>]+>", " ", body))
        txt = re.sub(r"[ \t]+", " ", txt)
        txt = re.sub(r"\n\s*\n+", "\n\n", txt).strip()
        return _add(c, txt, title, url, kw.get("tags"))

    if action == "search":
        q = (kw.get("query") or "").strip()
        if not q:
            raise ValueError("search needs 'query'")
        # sanitise into an FTS OR query of bare terms/phrases
        terms = re.findall(r"[\w']+", q)
        if not terms:
            raise ValueError("no searchable terms in query")
        fts = " OR ".join('"%s"' % t for t in terms)
        rows = c.execute(
            "SELECT doc_id, idx, title, tags, snippet(chunks,0,'[',']','…',25), bm25(chunks), body "
            "FROM chunks WHERE chunks MATCH ? ORDER BY bm25(chunks) LIMIT ?",
            (fts, limit)).fetchall()
        tagf = (kw.get("tags") or "").strip().lower()
        out = []
        for d, i, t, tg, sn, sc, body in rows:
            if tagf and tagf not in (tg or "").lower():
                continue
            out.append({"doc_id": d, "chunk": i, "title": t, "tags": tg,
                        "score": round(-sc, 3), "snippet": sn,
                        "text": body[:700]})
        return {"query": q, "hits": len(out), "results": out}

    if action == "get":
        did = kw.get("doc_id")
        if not did:
            raise ValueError("get needs 'doc_id'")
        r = c.execute("SELECT id,title,source,tags,text,created FROM docs WHERE id=?", (did,)).fetchone()
        if not r:
            raise ValueError("no doc %s" % did)
        return {"doc_id": r[0], "title": r[1], "source": r[2], "tags": r[3],
                "created": time.strftime("%Y-%m-%d %H:%M", time.localtime(r[5])), "text": r[4]}

    if action == "list":
        rows = c.execute("SELECT id,title,source,tags,length(text),created FROM docs "
                         "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{"doc_id": a, "title": b, "source": cc, "tags": d, "chars": e,
                 "created": time.strftime("%Y-%m-%d %H:%M", time.localtime(f))}
                for a, b, cc, d, e, f in rows]

    if action == "delete":
        did = kw.get("doc_id")
        if not did:
            raise ValueError("delete needs 'doc_id'")
        c.execute("DELETE FROM docs WHERE id=?", (did,))
        c.execute("DELETE FROM chunks WHERE doc_id=?", (did,))
        c.commit()
        return {"deleted": did}

    if action == "stats":
        d = c.execute("SELECT COUNT(*), COALESCE(SUM(length(text)),0) FROM docs").fetchone()
        ch = c.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        return {"docs": d[0], "chars": d[1], "chunks": ch,
                "db": DB, "db_bytes": os.path.getsize(DB) if os.path.exists(DB) else 0}

    raise ValueError("unknown action: %s" % action)
