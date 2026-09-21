"""Does an unauthenticated GET fully warm the path, or only the socket?

Each trial uses a brand new client (new connection). Three prewarm strategies, interleaved so
time of day cannot favour one. Measures the FIRST real evaluation after the prewarm, then the
second and third on the same connection as the steady-state reference. Raw samples are saved.
"""
import asyncio, json, pathlib, statistics as st, time
from hyprsay.jev import Boolean, Choice, JevClient, load_key

Q = {"intent": Choice("Which desktop action does the speaker request?",
                      {"launch_app": "Start an application", "close_window": "Close a window", "none": "Not a desktop command"}),
     "addressed": Boolean("Is the utterance an instruction to a computer?")}
TINY = {"q": Boolean("Is this a greeting?")}

async def trial(strategy: str, key: str) -> dict:
    async with JevClient(key, deadline=8.0) as c:
        t = time.perf_counter()
        if strategy == "get":
            await c.warm()
        elif strategy == "get+authed":
            await c.warm(); await c.evaluate("hello", TINY)
        prewarm_ms = (time.perf_counter() - t) * 1000
        calls = []
        for _ in range(3):
            try: calls.append(round((await c.evaluate({"utterance": "open the file manager"}, Q)).latency_ms))
            except Exception as e: calls.append(type(e).__name__)
        return {"strategy": strategy, "prewarm_ms": round(prewarm_ms), "first": calls[0], "second": calls[1], "third": calls[2]}

async def main():
    key, rows = load_key(), []
    for _ in range(10):
        for s in ("none", "get", "get+authed"):
            rows.append(await trial(s, key)); await asyncio.sleep(0.3)
    out = pathlib.Path(__file__).with_name("prewarm_out.json"); out.write_text(json.dumps(rows, indent=1))
    med = lambda xs: round(st.median(xs)) if xs else None
    print(f"{'strategy':12} {'n':>3} {'first call p50':>15} {'min..max':>13} {'2nd p50':>8} {'3rd p50':>8} {'prewarm p50':>12}")
    for s in ("none", "get", "get+authed"):
        r = [x for x in rows if x["strategy"] == s]; f = [x["first"] for x in r if isinstance(x["first"], int)]
        print(f"{s:12} {len(f):>3} {med(f):>15} {f'{min(f)}..{max(f)}':>13} {med([x['second'] for x in r if isinstance(x['second'], int)]):>8} {med([x['third'] for x in r if isinstance(x['third'], int)]):>8} {med([x['prewarm_ms'] for x in r]):>12}")
    print("failures:", sum(1 for x in rows for k in ("first", "second", "third") if not isinstance(x[k], int)))
asyncio.run(main())
