import asyncio, json, os, random, statistics as st, time, httpx
KEY = open(os.path.expanduser('~/.config/hyprvoice/ai-gateway.key')).read().strip()
B = 'https://ai-gateway.vercel.sh/typesafe/v1/systemone'
rng = random.Random(7); WORDS = 'notes draft invoice report hyprland config dotfiles meeting budget roadmap server deploy logs inbox playlist'.split()
def wins(n): return [{"id": f"w{i}", "class": "kitty", "title": ' '.join(rng.choice(WORDS) for _ in range(6)), "workspace": 1 + i % 9} for i in range(n)]
noul = {"type": "noul", "instructions": "Is `heard` an instruction addressed to the computer?"}
small = {"model": "typesafe-ai/jev", "state": {"heard": "put the browser on workspace three", "windows": wins(3)}, "questions": {"q": noul}}
large = {"model": "typesafe-ai/jev", "state": {"heard": "put the browser on workspace three", "windows": wins(260)}, "questions": {"q": noul}}
TIE_STATE = {"heard": "put the browser on workspace three", "focused_window": "w2", "windows": [{"id": "w1", "class": "firefox", "title": "Hyprland Wiki"}, {"id": "w2", "class": "kitty", "title": "nvim server.py"}]}
TIE_Q = {"type": "choice", "instructions": "Which window does the speaker want to act on? Use focused_window when they do not name one.", "criteria": {"w1": "firefox: Hyprland Wiki", "w2": "kitty: nvim server.py"}}
async def main():
    out = {}
    async with httpx.AsyncClient(http2=True, headers={"Authorization": f"Bearer {KEY}"}, timeout=15) as c:
        await c.post(B, json=small)
        # (a) interleave small and large so time-of-day cannot masquerade as a size effect
        res = {"small": [], "large": []}
        for _ in range(25):
            for name, body in (("small", small), ("large", large)):
                r = await c.post(B, json=body); res[name].append(r.status_code)
        out["interleaved_503"] = {k: {"n": len(v), "ok": v.count(200), "503": v.count(503), "other": [s for s in v if s not in (200, 503)]} for k, v in res.items()}
        # (b) self-ensemble: the SAME near-tie question under 8 ids in ONE request
        K = 8; singles, means, within_spread, flips_single, flips_mean = [], [], [], 0, 0
        for _ in range(20):
            r = await c.post(B, json={"model": "typesafe-ai/jev", "state": TIE_STATE, "questions": {f"t{i}": TIE_Q for i in range(K)}})
            if r.status_code != 200: continue
            ps = [a["probabilities"]["w1"] for a in r.json()["answers"].values()]
            singles.append(ps[0]); means.append(st.mean(ps)); within_spread.append(max(ps) - min(ps))
            flips_single += ps[0] < 0.5; flips_mean += st.mean(ps) < 0.5
        out["self_ensemble"] = {"requests_ok": len(singles), "copies_per_request": K,
            "copies_identical_within_a_request": all(s == 0 for s in within_spread), "within_request_spread_max": round(max(within_spread), 2), "within_request_spread_median": round(st.median(within_spread), 2),
            "single_copy": {"min": min(singles), "max": max(singles), "stdev": round(st.stdev(singles), 3), "picked_w2": flips_single},
            "mean_of_8": {"min": round(min(means), 3), "max": round(max(means), 3), "stdev": round(st.stdev(means), 3), "picked_w2": flips_mean}}
    json.dump(out, open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'followup_out.json'), 'w'), indent=1); print(json.dumps(out, indent=1))
asyncio.run(main())
