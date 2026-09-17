import os, datetime

PATH = "/data/context/context.md"

def run(action="read", text=None, section=None, max_chars=20000):
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    if not os.path.exists(PATH):
        open(PATH, "w").write("# Context memory\n")
    if action == "read":
        return open(PATH).read()[-max_chars:]
    if action in ("append", "note"):
        if not text:
            raise ValueError("text is required for append")
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        with open(PATH, "a") as f:
            if section:
                f.write(f"\n## {section}\n")
            f.write(f"- ({stamp}) {text}\n")
        return "appended"
    if action == "write":
        if text is None:
            raise ValueError("text is required for write")
        open(PATH, "w").write(text)
        return "written (%d chars)" % len(text)
    raise ValueError("unknown action: %s" % action)
