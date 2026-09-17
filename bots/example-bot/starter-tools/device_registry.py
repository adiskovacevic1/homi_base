import json, os
DIR = "/data/device_registry"
PATH = os.path.join(DIR, "devices.json")

def _load():
    try:
        with open(PATH) as f:
            return json.load(f)
    except Exception:
        return []

def _save(d):
    os.makedirs(DIR, exist_ok=True)
    with open(PATH, "w") as f:
        json.dump(d, f, indent=1)

def run(action, ip=None, name=None, kind=None, model=None, room=None, notes=None, query=None):
    devs = _load()
    if action == "list":
        return devs
    if action == "add":
        if not ip:
            raise ValueError("add requires ip")
        rec = {"ip": ip, "name": name, "kind": kind, "model": model, "room": room, "notes": notes}
        devs = [d for d in devs if d.get("ip") != ip]
        devs.append(rec)
        _save(devs)
        return rec
    if action == "remove":
        if not (ip or name):
            raise ValueError("remove requires ip or name")
        keep = [d for d in devs if d.get("ip") != ip and (not name or d.get("name") != name)]
        _save(keep)
        return {"removed": len(devs) - len(keep), "remaining": len(keep)}
    if action == "get":
        q = (query or name or ip or "").lower()
        if not q:
            raise ValueError("get requires query")
        return [d for d in devs if any(q in str(v).lower() for v in d.values())]
    raise ValueError("unknown action: %s" % action)
