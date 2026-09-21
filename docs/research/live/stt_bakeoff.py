"""Speech to text bake-off: local models on an idle machine vs every gateway batch model.

Same eight command clips through each. Records latency and the transcript. Never prints the key.
Run with the scratch venv that has sherpa-onnx and numpy; httpx is not needed (stdlib only).
"""
import base64, glob, http.client, io, json, os, ssl, statistics as st, sys, time, wave
import numpy as np

NPZ, MODELS = sys.argv[1], os.path.expanduser('~/.cache/hyprsay/models')
KEY = open(os.path.expanduser('~/.config/hyprsay/ai-gateway.key')).read().strip()
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'stt_bakeoff_out.json')
z = np.load(NPZ, allow_pickle=True); SR = int(z['sr']); CLIPS = [z[f'a{i}'] for i in range(8)]

def wav16k(a):
    n = int(len(a) * 16000 / SR); x = np.interp(np.linspace(0, len(a) - 1, n), np.arange(len(a)), a)
    pcm = (np.clip(x, -1, 1) * 32767).astype('<i2').tobytes(); b = io.BytesIO()
    with wave.open(b, 'wb') as w: w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(pcm)
    return b.getvalue()
WAVS = [wav16k(a) for a in CLIPS]
R = {"meta": {"loadavg_start": open('/proc/loadavg').read().split()[:3], "clip_seconds": [round(len(a) / SR, 2) for a in CLIPS]}, "local": {}, "gateway": {}}
def save(): json.dump(R, open(OUT, 'w'), indent=1)

# ---- local, one thread, idle machine
import sherpa_onnx as so
pick = lambda d, p: sorted(glob.glob(os.path.join(d, p)))[0]
def local(name, rec):
    rows = []
    for rep in range(3):
        for i, a in enumerate(CLIPS):
            s = rec.create_stream(); t = time.perf_counter(); s.accept_waveform(SR, a); rec.decode_stream(s)
            rows.append({"clip": i, "ms": round((time.perf_counter() - t) * 1000), "text": s.result.text.strip()})
    R["local"][name] = rows; save()
d = os.path.join(MODELS, 'sherpa-onnx-moonshine-tiny-en-quantized-2026-02-27')
local('moonshine-tiny', so.OfflineRecognizer.from_moonshine_v2(encoder=os.path.join(d, 'encoder_model.ort'), decoder=os.path.join(d, 'decoder_model_merged.ort'), tokens=os.path.join(d, 'tokens.txt'), num_threads=1))
d = os.path.join(MODELS, 'sherpa-onnx-nemo-parakeet_tdt_transducer_110m-en-36000-int8')
local('parakeet-110m', so.OfflineRecognizer.from_transducer(tokens=os.path.join(d, 'tokens.txt'), encoder=pick(d, 'encoder*.onnx'), decoder=pick(d, 'decoder*.onnx'), joiner=pick(d, 'joiner*.onnx'), num_threads=1, model_type='nemo_transducer'))

# ---- gateway batch route, one warm HTTP/1.1 keepalive connection per model
GW = ['google/gemini-3.5-transcribe', 'openai/whisper-1', 'openai/gpt-4o-mini-transcribe', 'openai/gpt-4o-transcribe',
      'spacexai/grok-stt', 'fish-audio/transcribe-1', 'google/gemini-3.5-transcribe-live', 'openai/gpt-realtime-whisper']
for model in GW:
    conn = http.client.HTTPSConnection('ai-gateway.vercel.sh', timeout=30, context=ssl.create_default_context()); rows = []
    try:
        conn.request('GET', '/typesafe/v1/models'); conn.getresponse().read()      # open the socket first, like the app will on key down
        for rep in range(3):
            for i, wav in enumerate(WAVS):
                body = json.dumps({"audio": base64.b64encode(wav).decode(), "mediaType": "audio/wav"})
                t = time.perf_counter()
                conn.request('POST', '/v4/ai/transcription-model', body=body, headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json",
                    "ai-gateway-protocol-version": "0.0.1", "ai-transcription-model-specification-version": "4", "ai-model-id": model})
                resp = conn.getresponse(); raw = resp.read(); ms = round((time.perf_counter() - t) * 1000)
                if resp.status != 200:
                    rows.append({"clip": i, "ms": ms, "status": resp.status, "error": raw[:220].decode('utf8', 'replace')})
                    if rep == 0 and i == 0: break                                   # model not usable on this route: do not hammer it
                else:
                    j = json.loads(raw); rows.append({"clip": i, "ms": ms, "text": (j.get("text") or "").strip(), "keys": sorted(j.keys())})
            if rows and "error" in rows[0]: break
    except Exception as e:
        rows.append({"exception": f"{type(e).__name__}: {e}"[:200]})
    finally:
        conn.close()
    R["gateway"][model] = rows; save()
R["meta"]["loadavg_end"] = open('/proc/loadavg').read().split()[:3]; save()

# ---- report
def summ(rows):
    ms = sorted(r["ms"] for r in rows if "text" in r)
    return f"n={len(ms):2} p50={round(st.median(ms)):4} min={ms[0]:4} max={ms[-1]:5}" if ms else "no successful calls"
print("loadavg", R["meta"]["loadavg_start"], "->", R["meta"]["loadavg_end"], "| clip seconds", R["meta"]["clip_seconds"])
for group in ("local", "gateway"):
    for name, rows in R[group].items():
        print(f"{group:8} {name:36} {summ(rows)}" + ("" if any("text" in r for r in rows) else "   " + str(rows[0])[:170]))
print("\ntranscripts, first pass (clip: model -> text)")
for i in range(8):
    print(f" clip {i}")
    for group in ("local", "gateway"):
        for name, rows in R[group].items():
            t = next((r["text"] for r in rows if r.get("clip") == i and "text" in r), None)
            if t is not None: print(f"    {name:34} {t!r}")
