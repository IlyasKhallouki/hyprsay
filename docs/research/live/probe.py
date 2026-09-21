import json, os, time, statistics, httpx
KEY = open(os.path.expanduser('~/.config/hyprvoice/ai-gateway.key')).read().strip()
A = 'https://ai-gateway.vercel.sh/v1/evaluate'
B = 'https://ai-gateway.vercel.sh/typesafe/v1/systemone'
state = {
  "heard": "put the browser on workspace three",
  "focused_window": "w2",
  "windows": [
    {"id": "w1", "class": "firefox", "title": "Hyprland Wiki - Dispatchers - Mozilla Firefox", "workspace": 1},
    {"id": "w2", "class": "kitty", "title": "nvim src/hypruse/server.py", "workspace": 1},
    {"id": "w3", "class": "org.gnome.Nautilus", "title": "Downloads", "workspace": 2},
    {"id": "w4", "class": "Spotify", "title": "Spotify Premium", "workspace": 4},
    {"id": "w5", "class": "kitty", "title": "htop", "workspace": 2}],
  "workspaces": [1, 2, 4], "active_workspace": 1}
INTENTS = {
  "focus_window": "Switch focus to a window", "close_window": "Close a window",
  "move_to_workspace": "Move a window to a different workspace", "switch_workspace": "Go to / show a different workspace",
  "toggle_fullscreen": "Make a window fullscreen or leave fullscreen", "toggle_floating": "Float or tile a window",
  "launch_app": "Start an application that is not running", "resize_window": "Make a window bigger or smaller",
  "swap_windows": "Swap the positions of two windows", "type_text": "Dictate / type literal text into the focused app",
  "scroll": "Scroll the content of a window", "volume": "Change or mute audio volume",
  "screenshot": "Take a screenshot", "lock_screen": "Lock the session",
  "none": "Not a desktop command: ambient speech, conversation, or noise"}
def qs(booltype):
    return {
      "intent": {"type": "choice", "instructions": "Which desktop action is the speaker asking for in `heard`?", "criteria": INTENTS},
      "target": {"type": "choice", "instructions": "Which window in `windows` does the speaker want to act on? Use `focused_window` when they do not name one.",
                 "criteria": {w["id"]: f'{w["class"]}: {w["title"]}' for w in state["windows"]}},
      "workspace": {"type": "choice", "instructions": "Which workspace number does the speaker name as the destination?",
                    "criteria": {str(n): f"workspace {n}" for n in range(1, 11)} | {"none": "no workspace named"}},
      "is_command": {"type": booltype, "instructions": "Is `heard` an instruction addressed to the computer to control the desktop?"},
      "destructive": {"type": "score", "instructions": "How much unsaved work could be lost if this action ran by mistake?",
                      "criteria": ["Nothing can be lost", "Minor, easily undone", "A window closes", "The whole session ends"]}}
def run(c, url, body, n, tag):
    ts, last = [], None
    for i in range(n):
        t = time.perf_counter(); r = c.post(url, json=body); dt = (time.perf_counter() - t) * 1000
        ts.append(dt); last = r
        if r.status_code != 200: print(tag, 'HTTP', r.status_code, r.text[:400]); return None, ts
    print(f"{tag}: http/{last.http_version} first={ts[0]:.0f}ms warm={[round(x) for x in ts[1:]]} median_warm={statistics.median(ts[1:]):.0f}ms" if n > 1 else f"{tag}: {ts[0]:.0f}ms")
    return last.json(), ts
out = {}
with httpx.Client(http2=True, headers={"Authorization": f"Bearer {KEY}"}, timeout=20) as c:
    tiny = {"model": "typesafe-ai/jev", "state": "put the browser on workspace three",
            "questions": {"q": {"type": "boolean", "instructions": "Is this a request to move a window?"}}}
    out['A_tiny'], _ = run(c, A, tiny, 6, 'routeA tiny(1 boolean)')
    out['A_full'], tA = run(c, A, {"model": "typesafe-ai/jev", "state": state, "questions": qs("boolean")}, 8, 'routeA full(5 questions)')
    out['B_full'], tB = run(c, B, {"model": "typesafe-ai/jev", "state": state, "questions": qs("noul")}, 8, 'routeB full(5 questions)')
    # determinism: 6 identical requests, compare every probability
    reps = [c.post(A, json={"model": "typesafe-ai/jev", "state": state, "questions": qs("boolean")}).json()["answers"] for _ in range(6)]
    out['determinism_reps'] = reps
    same = all(json.dumps(r, sort_keys=True) == json.dumps(reps[0], sort_keys=True) for r in reps)
    print("determinism: 6 identical requests byte-identical answers =", same)
    if not same:
        for q in reps[0]:
            vals = {json.dumps(r[q], sort_keys=True) for r in reps}
            if len(vals) > 1: print("  varies:", q, [ (r[q].get('probability'), r[q].get('score'), r[q].get('choice')) for r in reps])
json.dump(out, open(os.path.join(os.path.dirname(__file__), 'probe_out.json'), 'w'), indent=1)
for k in ('A_full', 'B_full'):
    if out.get(k): print(f"\n== {k}\n", json.dumps(out[k], indent=1)[:2600])
