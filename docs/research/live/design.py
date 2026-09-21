import asyncio, json, os, statistics as st, httpx
KEY = open(os.path.expanduser('~/.config/hyprvoice/ai-gateway.key')).read().strip()
B = 'https://ai-gateway.vercel.sh/typesafe/v1/systemone'
STATE = {"heard": "put the browser on workspace three", "focused_window": "w2"}
OLD_I = "Which window does the speaker want to act on? Use focused_window when they do not name one."
NEW_I = "Which window is the speaker referring to in `heard`?"
bare = {"w1": "firefox: Hyprland Wiki", "w2": "kitty: nvim server.py"}
rich = {"w1": {"app": "Firefox", "kind": "web browser", "title": "Hyprland Wiki"}, "w2": {"app": "kitty", "kind": "terminal emulator", "title": "nvim server.py"}}
rich_fb = dict(rich, focused={"what": "The speaker names no window, or says this / it / here. Means the currently focused window."})
VARIANTS = {"A baseline (bare options, competing instruction)": (OLD_I, bare), "B rich options, same instruction": (OLD_I, rich),
            "C rich options, neutral instruction": (NEW_I, rich), "D rich options, neutral instruction, explicit 'focused' option": (NEW_I, rich_fb)}
# D must not steal unambiguous cases, and must catch deictic ones
PROBES = {"put the browser on workspace three": "w1", "move this to workspace three": "focused", "close the terminal": "w2", "make it fullscreen": "focused"}
async def main():
    async with httpx.AsyncClient(http2=True, headers={"Authorization": f"Bearer {KEY}"}, timeout=15) as c:
        print("== same utterance, four question designs, 12 identical requests each")
        for name, (ins, crit) in VARIANTS.items():
            ps, picks = [], []
            for _ in range(12):
                r = await c.post(B, json={"model": "typesafe-ai/jev", "state": STATE, "questions": {"t": {"type": "choice", "instructions": ins, "criteria": crit}}})
                if r.status_code == 200: a = r.json()["answers"]["t"]; ps.append(a["probabilities"]["w1"]); picks.append(a["choice"])
            print(f"  {name}\n      P(firefox) min={min(ps):.2f} median={st.median(ps):.2f} max={max(ps):.2f} | correct picks {picks.count('w1')}/{len(picks)}")
        print("== design D on four utterances (6 requests each): expected answer, picks, P(expected) range")
        for heard, want in PROBES.items():
            ps, picks = [], []
            for _ in range(6):
                r = await c.post(B, json={"model": "typesafe-ai/jev", "state": dict(STATE, heard=heard), "questions": {"t": {"type": "choice", "instructions": NEW_I, "criteria": rich_fb}}})
                if r.status_code == 200: a = r.json()["answers"]["t"]; ps.append(a["probabilities"][want]); picks.append(a["choice"])
            print(f"  {heard!r:42} want={want:8} correct {picks.count(want)}/{len(picks)}  P(want) {min(ps):.2f}..{max(ps):.2f}")
asyncio.run(main())
