"""Day-one Jev validation from this machine. Synthetic desktop data only. Never prints the key."""
import asyncio, glob, json, os, random, re, statistics as st, time, httpx
KEY = open(os.path.expanduser('~/.config/hyprvoice/ai-gateway.key')).read().strip()
A = 'https://ai-gateway.vercel.sh/v1/evaluate'; B = 'https://ai-gateway.vercel.sh/typesafe/v1/systemone'
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'dayone_out.json')
R = {"meta": {"started": time.strftime('%Y-%m-%dT%H:%M:%S%z')}, "errors": []}
rng = random.Random(7)
APPS = []
for f in sorted(glob.glob('/usr/share/applications/*.desktop')):
    try: txt = open(f, errors='ignore').read()
    except OSError: continue
    if 'NoDisplay=true' in txt: continue
    m = re.search(r'^Name=(.+)$', txt, re.M)
    if m: APPS.append((os.path.basename(f)[:-8], m.group(1).strip()))
CLASSES = ['firefox', 'kitty', 'org.gnome.Nautilus', 'Spotify', 'code', 'discord', 'obsidian', 'thunderbird', 'mpv', 'zathura']
WORDS = 'notes draft invoice report hyprland config dotfiles meeting budget roadmap server deploy logs inbox playlist tutorial issue review design specs'.split()
def windows(n): return [{"id": f"w{i}", "class": CLASSES[i % len(CLASSES)], "title": ' '.join(rng.choice(WORDS) for _ in range(6)), "workspace": 1 + i % 9} for i in range(n)]
INTENTS = {"focus_window": "Switch focus to a window", "close_window": "Close a window", "move_to_workspace": "Move a window to a different workspace",
  "switch_workspace": "Show a different workspace", "toggle_fullscreen": "Make a window fullscreen or leave fullscreen", "toggle_floating": "Float or tile a window",
  "launch_app": "Start an application", "resize_window": "Make a window bigger or smaller", "type_text": "Type literal dictated text", "volume": "Change audio volume",
  "none": "Not a desktop command: ambient speech or conversation"}
def q_intent(): return {"type": "choice", "instructions": "Which desktop action is the speaker asking for in `heard`?", "criteria": INTENTS}
def q_bool(i): return {"type": "noul", "instructions": f"Is `heard` an instruction addressed to the computer? (variant {i})"}
def state(nwin, heard="put the browser on workspace three"): return {"heard": heard, "focused_window": "w1", "windows": windows(nwin)}
def summ(ts):
    ts = sorted(ts); n = len(ts)
    return {"n": n, "p50": round(st.median(ts)), "p90": round(ts[int(.9 * (n - 1))]), "max": round(ts[-1]), "min": round(ts[0])} if ts else {"n": 0}
async def call(c, url, body, tag):
    t = time.perf_counter()
    try:
        r = await c.post(url, json=body); dt = (time.perf_counter() - t) * 1000
        if r.status_code != 200:
            R["errors"].append({"tag": tag, "status": r.status_code, "ms": round(dt), "body": r.text[:160]}); return None, dt
        return r.json(), dt
    except Exception as e:
        R["errors"].append({"tag": tag, "exc": type(e).__name__, "ms": round((time.perf_counter() - t) * 1000)}); return None, None
def prov_ms(j):
    try:
        a = (j.get('provider_metadata') or j.get('providerMetadata'))['gateway']['routing']['modelAttempts'][0]['providerAttempts'][0]; return a['endTime'] - a['startTime']
    except Exception: return None
async def series(c, url, body, n, tag):
    ts, ps, toks = [], [], None
    for _ in range(n):
        j, dt = await call(c, url, body, tag)
        if j is not None:
            ts.append(dt); p = prov_ms(j); ps.append(p) if p is not None else None
            u = j.get('usage') or {}; toks = u.get('input_tokens', u.get('inputTokens'))
    return {"total_ms": summ(ts), "provider_ms": summ(ps), "input_tokens": toks}
async def main():
    async with httpx.AsyncClient(http2=True, headers={"Authorization": f"Bearer {KEY}"}, timeout=15) as c:
        await call(c, B, {"model": "typesafe-ai/jev", "state": "warm", "questions": {"q": q_bool(0)}}, 'warm')
        # 1. latency vs payload size (5 questions fixed)
        R["latency_vs_tokens"] = {}
        for nwin in (3, 40, 130, 260):
            body = {"model": "typesafe-ai/jev", "state": state(nwin), "questions": {"intent": q_intent(), **{f"b{i}": q_bool(i) for i in range(4)}}}
            R["latency_vs_tokens"][f"{nwin}_windows"] = await series(c, B, body, 30, f'tok{nwin}'); save()
        # 2. latency vs question count (small state)
        R["latency_vs_questions"] = {}
        for nq in (1, 5, 15, 40):
            body = {"model": "typesafe-ai/jev", "state": state(5), "questions": {f"b{i}": q_bool(i) for i in range(nq)}}
            R["latency_vs_questions"][str(nq)] = await series(c, B, body, 30, f'nq{nq}'); save()
        # 3. route A vs route B, interleaved to cancel drift
        bA = {"model": "typesafe-ai/jev", "state": state(5), "questions": {"intent": q_intent(), "cmd": {"type": "boolean", "instructions": q_bool(0)["instructions"]}}}
        bB = {"model": "typesafe-ai/jev", "state": state(5), "questions": {"intent": q_intent(), "cmd": q_bool(0)}}
        ta, tb = [], []
        for _ in range(30):
            ja, da = await call(c, A, bA, 'routeA'); jb, db = await call(c, B, bB, 'routeB')
            if ja: ta.append(da)
            if jb: tb.append(db)
        R["route_A_vs_B"] = {"A": summ(ta), "B": summ(tb)}; save()
        # 4. determinism: 30 byte-identical requests, near-tie target question included
        body = {"model": "typesafe-ai/jev", "state": {"heard": "put the browser on workspace three", "focused_window": "w2", "windows": [
            {"id": "w1", "class": "firefox", "title": "Hyprland Wiki"}, {"id": "w2", "class": "kitty", "title": "nvim server.py"}]},
            "questions": {"intent": q_intent(), "cmd": q_bool(0), "target": {"type": "choice", "instructions": "Which window does the speaker want to act on? Use focused_window when they do not name one.", "criteria": {"w1": "firefox: Hyprland Wiki", "w2": "kitty: nvim server.py"}}}}
        reps = [j["answers"] for j in [(await call(c, B, body, 'det'))[0] for _ in range(30)] if j]
        R["determinism"] = {"n": len(reps), "target_p_w1": sorted({r["target"]["probabilities"]["w1"] for r in reps}), "target_choice_counts": {k: sum(r["target"]["choice"] == k for r in reps) for k in ("w1", "w2")},
            "intent_p_top": sorted({r["intent"]["probabilities"]["move_to_workspace"] for r in reps}), "cmd_noul": sorted({r["cmd"]["noul"] for r in reps})}; save()
        # 5. independence: the same question alone vs riding with 20 others (5 reps each)
        solo = {"model": "typesafe-ai/jev", "state": body["state"], "questions": {"target": body["questions"]["target"]}}
        crowd = {"model": "typesafe-ai/jev", "state": body["state"], "questions": {"target": body["questions"]["target"], **{f"b{i}": q_bool(i) for i in range(20)}}}
        ps_, pc_ = [], []
        for _ in range(8):
            j1, _ = await call(c, B, solo, 'solo'); j2, _ = await call(c, B, crowd, 'crowd')
            if j1: ps_.append(j1["answers"]["target"]["probabilities"]["w1"])
            if j2: pc_.append(j2["answers"]["target"]["probabilities"]["w1"])
        R["independence"] = {"p_w1_alone": ps_, "p_w1_among_20": pc_}; save()
        # 6. option-order sensitivity: 100-app choice, permuted 10 times
        apps = APPS[:99]; picks = []
        for k in range(10):
            order = apps[:]; random.Random(k).shuffle(order)
            crit = {aid: name for aid, name in order} | {"none": "no application is named"}
            j, _ = await call(c, B, {"model": "typesafe-ai/jev", "state": {"heard": "open the file manager"}, "questions": {"app": {"type": "choice", "instructions": "Which installed application does the speaker want to open?", "criteria": crit}}}, 'perm')
            if j: a = j["answers"]["app"]; top = sorted(a["probabilities"].items(), key=lambda kv: -kv[1])[:3]; picks.append({"choice": a["choice"], "top3": top})
        R["option_order"] = {"n_options": len(apps) + 1, "distinct_choices": sorted({p["choice"] for p in picks}), "runs": picks}; save()
        # 7. multiplexing: 3 concurrent requests on one h2 connection vs 1
        conc = []
        for _ in range(12):
            t = time.perf_counter(); res = await asyncio.gather(*[call(c, B, bB, 'conc') for _ in range(3)])
            if all(r[0] for r in res): conc.append((time.perf_counter() - t) * 1000)
        R["concurrent_3_wall_ms"] = summ(conc); save()
    R["meta"]["finished"] = time.strftime('%Y-%m-%dT%H:%M:%S%z'); R["meta"]["n_apps_found"] = len(APPS); save()
def save(): json.dump(R, open(OUT, 'w'), indent=1)
asyncio.run(main()); print("done; errors:", len(R["errors"]))
