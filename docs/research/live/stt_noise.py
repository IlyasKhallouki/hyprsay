"""Who survives bad audio? Same 8 commands, degraded. Local vs the fastest gateway models.
Every network call has a hard timeout. Never prints the key."""
import base64, glob, http.client, io, json, os, re, ssl, sys, time, wave
import numpy as np, sherpa_onnx as so
KEY = open(os.path.expanduser('~/.config/hyprsay/ai-gateway.key')).read().strip()
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'stt_noise_out.json')
z = np.load(sys.argv[1], allow_pickle=True); SR = int(z['sr']); rng = np.random.default_rng(3)
CMDS = ["switch to workspace three", "open firefox", "close this window", "move window to workspace two", "focus kitty", "toggle fullscreen", "open obsidian", "scroll down"]
def norm(t):
    t = re.sub(r"[^a-z0-9 ]", "", t.lower().replace("full screen", "fullscreen"))
    for w, d in (("three", "3"), ("two", "2")): t = re.sub(rf"\b{w}\b", d, t)
    return " ".join(t.split())
def noisy(a, snr_db):
    p = np.mean(a ** 2); n = rng.standard_normal(len(a)).astype(np.float32); n = np.cumsum(n); n -= n.mean(); n /= (np.std(n) + 1e-9)   # brown-ish room noise
    w = rng.standard_normal(len(a)).astype(np.float32); mix = 0.7 * n + 0.3 * w; mix *= np.sqrt(p / (10 ** (snr_db / 10)) / np.mean(mix ** 2))
    return np.clip(a + mix, -1, 1).astype(np.float32)
def bad_mic(a):   # quiet, band-limited around 300-3400 Hz, plus light noise: a poor laptop mic across the room
    f = np.fft.rfft(a); hz = np.fft.rfftfreq(len(a), 1 / SR); f[(hz < 300) | (hz > 3400)] = 0
    return noisy((np.fft.irfft(f, len(a)) * 0.25).astype(np.float32), 12)
COND = {"clean": lambda a: a, "noise 10 dB SNR": lambda a: noisy(a, 10), "noise 3 dB SNR": lambda a: noisy(a, 3), "bad laptop mic": bad_mic}
def wav(a):
    n = int(len(a) * 16000 / SR); x = np.interp(np.linspace(0, len(a) - 1, n), np.arange(len(a)), a); b = io.BytesIO()
    with wave.open(b, 'wb') as w: w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes((np.clip(x, -1, 1) * 32767).astype('<i2').tobytes())
    return b.getvalue()
M = os.path.expanduser('~/.cache/hyprsay/models'); pick = lambda d, p: sorted(glob.glob(os.path.join(d, p)))[0]
d1 = os.path.join(M, 'sherpa-onnx-moonshine-tiny-en-quantized-2026-02-27'); d2 = os.path.join(M, 'sherpa-onnx-nemo-parakeet_tdt_transducer_110m-en-36000-int8')
LOCAL = {"local moonshine-tiny": so.OfflineRecognizer.from_moonshine_v2(encoder=os.path.join(d1, 'encoder_model.ort'), decoder=os.path.join(d1, 'decoder_model_merged.ort'), tokens=os.path.join(d1, 'tokens.txt'), num_threads=1),
         "local parakeet-110m": so.OfflineRecognizer.from_transducer(tokens=os.path.join(d2, 'tokens.txt'), encoder=pick(d2, 'encoder*.onnx'), decoder=pick(d2, 'decoder*.onnx'), joiner=pick(d2, 'joiner*.onnx'), num_threads=1, model_type='nemo_transducer')}
def cloud(model, w):
    c = http.client.HTTPSConnection('ai-gateway.vercel.sh', timeout=20, context=ssl.create_default_context())
    try:
        c.request('POST', '/v4/ai/transcription-model', body=json.dumps({"audio": base64.b64encode(w).decode(), "mediaType": "audio/wav"}), headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json", "ai-gateway-protocol-version": "0.0.1", "ai-transcription-model-specification-version": "4", "ai-model-id": model})
        r = c.getresponse(); raw = r.read(); return (json.loads(raw).get("text") or "") if r.status == 200 else f"<http {r.status}>"
    except Exception as e: return f"<{type(e).__name__}>"
    finally: c.close()
CLOUD = ["spacexai/grok-stt", "fish-audio/transcribe-1", "openai/gpt-4o-mini-transcribe"]
R = {}
for cname, fx in COND.items():
    clips = [fx(z[f'a{i}'].astype(np.float32)) for i in range(8)]; R[cname] = {}
    for name, rec in LOCAL.items():
        out = []
        for a in clips: s = rec.create_stream(); s.accept_waveform(SR, a); rec.decode_stream(s); out.append(s.result.text.strip())
        R[cname][name] = out
    for model in CLOUD: R[cname]["cloud " + model.split('/')[1]] = [cloud(model, wav(a)) for a in clips]
    json.dump(R, open(OUT, 'w'), indent=1)
names = list(R["clean"].keys()); print(f"{'commands right out of 8':26}" + "".join(f"{n.replace('local ', 'L:').replace('cloud ', 'C:'):>24}" for n in names))
for cname in COND: print(f"{cname:26}" + "".join(f"{sum(norm(t) == norm(c) for t, c in zip(R[cname][n], CMDS)):>24}" for n in names))
print("\nmisses:")
for cname in COND:
    for n in names:
        for t, c in zip(R[cname][n], CMDS):
            if norm(t) != norm(c): print(f"  [{cname}] {n}: wanted {c!r} got {t!r}")
