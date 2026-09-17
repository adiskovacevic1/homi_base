"""Speech-to-text sidecar. Loads the Whisper model once, then reads one WAV path per line on stdin and
prints one JSON line per file: {"path": ..., "text": ..., "seconds": ..., "language": ...}.
Node keeps this process alive for the life of the bot so no request pays the model-load cost."""
import json, os, sys, time
from faster_whisper import WhisperModel

model_name = os.environ.get("WHISPER_MODEL", "base.en")
model = WhisperModel(model_name, device="cpu", compute_type="int8", cpu_threads=max(2, (os.cpu_count() or 4) // 2))
print(json.dumps({"ready": True, "model": model_name}), flush=True)

for line in sys.stdin:
    path = line.strip()
    if not path:
        continue
    t0 = time.time()
    try:
        segments, info = model.transcribe(path, beam_size=2, vad_filter=True, language="en" if model_name.endswith(".en") else None,
                                          initial_prompt=os.environ.get("STT_PROMPT", "Hey bot. Dock bot. Hey bot, what is the pool status?"))
        text = " ".join(s.text.strip() for s in segments).strip()
        print(json.dumps({"path": path, "text": text, "seconds": round(time.time() - t0, 2), "language": info.language}), flush=True)
    except Exception as e:  # noqa
        print(json.dumps({"path": path, "error": str(e)}), flush=True)
