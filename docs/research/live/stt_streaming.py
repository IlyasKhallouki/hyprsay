"""Gateway STREAMING transcription, measured the way push to talk uses it.

Audio is paced in real time in 20 ms frames (as a microphone delivers it), then audio-done is
sent at "key release". The number that matters is audio-done -> final transcript. Also records
socket open -> stream-start, and first partial. Never prints the key. Protocol is experimental.
"""
import asyncio, json, os, statistics as st, sys, time, urllib.parse
import numpy as np, websockets

KEY = open(os.path.expanduser('~/.config/hyprsay/ai-gateway.key')).read().strip()
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'stt_streaming_out.json')
z = np.load(sys.argv[1], allow_pickle=True); SR = int(z['sr'])
def pcm(a, rate):
    n = int(len(a) * rate / SR); x = np.interp(np.linspace(0, len(a) - 1, n), np.arange(len(a)), a)
    return (np.clip(x, -1, 1) * 32767).astype('<i2').tobytes()
MODELS = {'google/gemini-3.5-transcribe-live': 16000, 'spacexai/grok-stt': 16000, 'openai/gpt-realtime-whisper': 24000}
R = {}

async def one(model, rate, audio):
    url = 'wss://ai-gateway.vercel.sh/v4/ai/transcription-model?ai-model-id=' + urllib.parse.quote(model, safe='')
    row = {"partials": 0}; t_open = time.perf_counter()
    try:
        async with websockets.connect(url, subprotocols=['ai-gateway-transcription.v1', f'ai-gateway-auth.{KEY}'], max_size=2**22, open_timeout=10) as ws:
            row["open_ms"] = round((time.perf_counter() - t_open) * 1000)
            await ws.send(json.dumps({"type": "transcription-stream.start", "inputAudioFormat": {"type": "audio/pcm", "rate": rate}}))
            done_at = None; first_partial = None; t_audio0 = None
            async def feed():
                nonlocal done_at, t_audio0
                frame = rate * 2 // 50; t_audio0 = time.perf_counter()                   # 20 ms of 16 bit mono
                for k, i in enumerate(range(0, len(audio), frame)):
                    await ws.send(audio[i:i + frame])
                    await asyncio.sleep(max(0, t_audio0 + (k + 1) * 0.02 - time.perf_counter()))
                await ws.send(json.dumps({"type": "transcription-stream.audio-done"})); done_at = time.perf_counter()
            feeder = asyncio.create_task(feed())
            async for msg in ws:
                if isinstance(msg, bytes): continue
                part = json.loads(msg); typ = part.get("type")
                if typ == "stream-start": row["stream_start_ms"] = round((time.perf_counter() - t_open) * 1000)
                elif typ in ("transcript-partial", "transcript-delta"):
                    row["partials"] += 1
                    if first_partial is None and t_audio0: first_partial = time.perf_counter(); row["first_partial_after_audio_start_ms"] = round((first_partial - t_audio0) * 1000)
                elif typ == "transcript-final" and done_at and "final_after_done_ms" not in row:
                    row["final_after_done_ms"] = round((time.perf_counter() - done_at) * 1000); row["text"] = part.get("text", "")
                elif typ == "finish":
                    row["finish_after_done_ms"] = round((time.perf_counter() - done_at) * 1000) if done_at else None
                    row.setdefault("text", part.get("text", "")); break
                elif typ == "error":
                    row["error"] = str(part.get("error"))[:200]; break
            feeder.cancel()
    except Exception as e:
        row["exception"] = f"{type(e).__name__}: {e}"[:220].replace(KEY, '<key>')
    return row

async def main():
    for model, rate in MODELS.items():
        rows = []
        for rep in range(2):
            for i in range(8):
                rows.append({"clip": i, **(await one(model, rate, pcm(z[f'a{i}'], rate)))})
                if rep == 0 and i == 0 and ("exception" in rows[0] or "error" in rows[0]): break
            if "exception" in rows[0] or "error" in rows[0]: break
        R[model] = rows; json.dump(R, open(OUT, 'w'), indent=1)
    med = lambda xs: round(st.median(xs)) if xs else None
    print(f"{'model':36} {'n':>3} {'audio-done->final p50':>22} {'min..max':>12} {'open':>6} {'1st partial':>12} {'partials/clip':>14}")
    for model, rows in R.items():
        ok = [r for r in rows if "text" in r]; fin = [r.get("final_after_done_ms", r.get("finish_after_done_ms")) for r in ok]; fin = [f for f in fin if f is not None]
        if not fin: print(f"{model:36}   - FAILED: {str(rows[0])[:150]}"); continue
        print(f"{model:36} {len(fin):>3} {med(fin):>22} {f'{min(fin)}..{max(fin)}':>12} {med([r['open_ms'] for r in ok]):>6} {str(med([r['first_partial_after_audio_start_ms'] for r in ok if 'first_partial_after_audio_start_ms' in r])):>12} {st.mean(r['partials'] for r in ok):>14.1f}")
        print("     texts:", [r["text"] for r in ok[:8]])
asyncio.run(main())
