import asyncio, os, httpx
KEY = open(os.path.expanduser('~/.config/hyprvoice/ai-gateway.key')).read().strip()
B = 'https://ai-gateway.vercel.sh/typesafe/v1/systemone'
I = "Which window is the speaker referring to in `heard`?"
crit = {"w1": {"app": "Firefox", "kind": "web browser", "title": "Hyprland Wiki"}, "w2": {"app": "kitty", "kind": "terminal emulator", "title": "nvim server.py"},
        "focused": {"what": "The speaker names no window, or says this / it / here. Means the currently focused window."}}
async def main():
    async with httpx.AsyncClient(http2=True, headers={"Authorization": f"Bearer {KEY}"}, timeout=15) as c:
        for focused in ("w2", "w1"):   # kitty focused (options overlap) vs firefox focused (no overlap)
            print(f"== 'close the terminal', focused_window={focused} ({'kitty: w2 and focused are the SAME window' if focused=='w2' else 'firefox: no overlap'})")
            for _ in range(5):
                r = await c.post(B, json={"model": "typesafe-ai/jev", "state": {"heard": "close the terminal", "focused_window": focused}, "questions": {"t": {"type": "choice", "instructions": I, "criteria": crit}}})
                if r.status_code != 200: print("   http", r.status_code); continue
                p = r.json()["answers"]["t"]["probabilities"]; kitty = p["w2"] + (p["focused"] if focused == "w2" else 0)
                print(f"   w1={p['w1']:.2f} w2={p['w2']:.2f} focused={p['focused']:.2f}  ->  merged P(kitty)={kitty:.2f}")
asyncio.run(main())
