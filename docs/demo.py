"""See the panel without a key: the real bridge and panel, a fake EVREN-like upstream and fake agents on this machine.

    python docs/demo.py [--lang en|tr]
    python docs/demo.py --screens        # writes the SVGs in docs/screens

Five keys start at different daily use: the first is already at its limit and gets parked, the others at 75, 50 and
25 percent, the last unused. About 16 agents, at most 8 active per key, run multi-turn sessions with tool calls; now
and then a request hits the per-minute limit or EVREN's hidden 503, the upstream drops a connection, or an agent hangs
up, and some agents come back after a pause. The statistics tab gets a week of generated history.
Nothing leaves 127.0.0.1 and the real log is not touched.
"""
import argparse
import datetime
import http.client
import io
import json
import os
import random
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import evren_bridge  # noqa: E402
import panel  # noqa: E402
import stats  # noqa: E402
from profiles import evren  # noqa: E402

SPEED = {"mimo-v2.6-pro": 75, "deepseek-v4.1-flash": 60, "glm-5.3": 45, "qwen3.8-flash-next": 90}  # tokens/s
TOOLS = ("bash", "read_file", "edit_file", "grep", "write_file", "list_dir")
DAILY = evren.DAILY_LIMIT
START = (1.0, 0.75, 0.5, 0.25, 0.0)  # each key's daily use when the demo starts
AGENTS = 16
CAP = 8  # active sessions per key, small so the active agents fit one page


class Upstream(BaseHTTPRequestHandler):
    """Streams answers like EVREN: reasoning, then text or a tool call, then usage; counts each key's daily tokens."""
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        key = self.headers["Authorization"].removeprefix("Bearer ")
        if self.path == "/v1/models":
            body = {"data": [{"id": m} for m in SPEED]}
        else:
            used = self.server.used.get(key, 0)
            body = {"used_tokens": int(used % 2_000_000), "cap": evren.WINDOW_LIMIT, "window_minutes": 5,
                    "daily_used_tokens": int(used), "daily_limit_tokens": DAILY,
                    "daily_remaining_tokens": int(DAILY - used), "daily_reset_at": "2099-01-01T00:00:00Z"}
        self.reply(200, json.dumps(body).encode(), "application/json")

    def do_POST(self):
        key = self.headers["Authorization"].removeprefix("Bearer ")
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.server.used.get(key, 0) >= DAILY:
            reset = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=120)).isoformat()
            body = json.dumps({"error": {"message": "daily limit", "code": "daily_token_limit_exceeded",
                                         "evren": {"resets_at": reset}}}).encode()
            return self.reply(429, body, "application/json")
        if random.random() < 0.02:  # the connection drops before an answer: the bridge sends a 502
            self.close_connection = True
            return
        if random.random() < 0.04:
            body = json.dumps({"error": {"message": "per-minute limit", "code": "rate_limit_exceeded"}}).encode()
            return self.reply(429, body, "application/json", {"Retry-After": "5"})
        turn, last = request["demo_turn"], request["demo_last"]
        prompt = 6000 + turn * 2500 + random.randint(0, 2000)
        cached = 0 if turn == 0 else prompt - random.randint(800, 3000)
        out = random.randint(60, 900)
        self.server.used[key] = self.server.used.get(key, 0) + prompt - cached + cached * evren.CACHE_WEIGHT + out
        used = self.server.used[key]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.send_header("X-Evren-Daily-Limit-Tokens", str(DAILY))
        self.send_header("X-Evren-Daily-Remaining-Tokens", str(int(DAILY - used)))
        self.send_header("X-Evren-Daily-Reset", "4070908800")
        self.end_headers()
        self.close_connection = True
        time.sleep(random.uniform(0.3, 1.8))  # first token
        if random.random() < 0.03:  # EVREN could not reach the model
            return self.chunk({}, finish="error", masked=True)
        steps = max(4, int(out / SPEED[request["model"]] * random.uniform(0.8, 1.2) / 0.25))
        tool = random.choice(TOOLS)
        for step in range(steps):  # reasoning first, then the answer or the tool call, at the model's speed
            if step < steps // 3:
                self.chunk({"reasoning": "…"})
            elif last:
                self.chunk({"content": "text "})
            else:
                self.chunk({"tool_calls": [{"index": 0, "function": {"name": tool, "arguments": "{}"}}]})
            time.sleep(0.25)
        self.chunk({}, finish="stop" if last else "tool_calls")
        self.line({"id": "x", "choices": [], "usage": {"prompt_tokens": prompt, "completion_tokens": out,
                                                       "prompt_tokens_details": {"cached_tokens": cached}}})
        self.wfile.write(b"data: [DONE]\n\n")

    def chunk(self, delta, finish=None, masked=False):
        self.line({**({} if masked else {"id": "x"}), "choices": [{"delta": delta, "finish_reason": finish}]})

    def line(self, data):
        self.wfile.write(b"data: " + json.dumps(data).encode() + b"\n\n")
        self.wfile.flush()

    def reply(self, status, body, kind, headers=()):
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        for name, value in dict(headers).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def agent(port, model, turns, stop):
    """One coding agent: a few turns with tool runs in between; a third come back once after a pause."""
    session = str(uuid.uuid4())
    for visit in range(2):
        for turn in range(turns):
            while not stop.is_set():
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
                body = json.dumps({"model": model, "messages": [], "stream": True, "demo_turn": turn,
                                   "demo_last": turn == turns - 1})
                conn.request("POST", "/v1/chat/completions", body,
                             {"Content-Type": "application/json", "X-Session-Affinity": session})
                resp = conn.getresponse()
                if random.random() < 0.02:  # the agent hangs up mid-answer and asks again
                    resp.read(64)
                    conn.close()
                    continue
                resp.read()
                conn.close()
                if resp.status not in (429, 502, 503):
                    break
                time.sleep(int(resp.getheader("Retry-After") or 3))
            if stop.is_set():
                return
            if turn < turns - 1:
                time.sleep(random.uniform(0.5, 6))  # the agent runs its tool
        if visit or random.random() > 0.35:
            return
        stop.wait(random.uniform(15, 50))  # its lead sends it back with a follow-up
        turns = random.randint(1, 2)


def crowd(port, size, stop):
    """Keep about `size` agents running, starting new ones as others finish."""
    running = []
    while not stop.is_set():
        running = [t for t in running if t.is_alive()]
        if len(running) < size:
            model = random.choices(list(SPEED), weights=(3, 4, 2, 2))[0]
            t = threading.Thread(target=agent, args=(port, model, random.randint(2, 7), stop), daemon=True)
            t.start()
            running.append(t)
        stop.wait(0.3)


def history(path, keys, days=7):
    """Generated log lines for the statistics tab: busier in working hours, sessions of several requests, a few
    errors, and the first key hitting its daily limit each evening."""
    now, lines, sessions = time.time(), [], []
    t = now - days * 86400
    while t < now - 120:
        hour = datetime.datetime.fromtimestamp(t).hour
        t += random.expovariate(1 / (12 if 9 <= hour < 19 else 80))
        stamp = datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if hour == 18 and random.random() < 0.05:
            lines.append(f"{stamp} {keys[0][0]} /v1/chat/completions model=glm-5.3 status=429 daily limit, parked "
                         f"until 00:00Z secs=0.010 profile=evren session={uuid.uuid4().hex[:8]}\n")
            continue
        if not sessions or random.random() < 0.15:
            sessions = (sessions + [uuid.uuid4().hex[:8]])[-30:]
        session = random.choice(sessions)
        model = random.choices(list(SPEED), weights=(3, 4, 2, 2))[0]
        prompt = random.randint(6000, 60000)
        k = random.choices(range(4), weights=(4, 3, 2, 1))[0]  # keys filled in order, so their shares step down
        cached, out = int(prompt * random.uniform(0.7, 0.97) * (1 - 0.08 * k)), random.randint(60, 900)
        secs = random.uniform(1, 14)
        status = random.choices((200, 429, 503, 502, 499), weights=(400, 4, 3, 1, 1))[0]
        line = f"{stamp} {keys[k][0]} /v1/chat/completions model={model} status={status}"
        if status == 200:
            line += f" prompt={prompt} cached={cached} out={out}"
        line += f" secs={secs:.3f} ttft={random.uniform(0.3, 2):.2f}"
        if status == 200:
            line += f" tps={SPEED[model] * random.uniform(0.7, 1.2):.0f}"
            if random.random() < 0.08:
                line += f" back={int(random.choice((random.uniform(5, 299), random.uniform(300, 3600))))}"
        lines.append(line + f" profile=evren session={session}\n")
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines)


def start(log, cap=CAP):
    """The fake upstream, the bridge with the EVREN profile, its statistics and the agents; returns what to stop."""
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    upstream.handle_error = lambda request, address: None  # agents that hang up are part of the show
    keys = [(f"key-{n}", f"demo-{n}") for n in range(1, len(START) + 1)]
    upstream.used = {key: share * DAILY for (_, key), share in zip(keys, START)}
    history(log, keys)
    evren_bridge.LOG_FILE, evren_bridge.ECHO = log, False
    server = evren_bridge.make_server(keys, f"http://127.0.0.1:{upstream.server_address[1]}", 0, cap, evren)
    record = stats.Stats.load(log, evren)
    record.attach(server.router)
    stop = threading.Event()
    for target, args in ((upstream.serve_forever, ()), (server.serve_forever, ()), (evren_bridge.poll, (server,)),
                         (crowd, (server.server_address[1], AGENTS, stop))):
        threading.Thread(target=target, args=args, daemon=True).start()
    return server, record, lambda: (stop.set(), server.shutdown(), upstream.shutdown())


def save(server, record, name, lang="en", tab=0, height=52):
    """Render one view at its own width and save it as docs/screens/<name>.svg."""
    from rich.console import Console
    view = panel.View(lang)
    view.tab, view.range = tab, 3  # statistics: 7 days, so the daily limits show
    renderable = panel.render(server.router, height, view=view, stats=record, profile=evren)
    probe = Console(file=io.StringIO(), width=220, record=True)
    probe.print(renderable)
    width = max(len(line.rstrip()) for line in probe.export_text().splitlines())
    console = Console(file=io.StringIO(), width=width, record=True, color_system="truecolor")
    console.print(panel.render(server.router, height, view=view, stats=record, profile=evren))
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "screens", f"{name}.svg")
    console.save_svg(path, title="evren-bridge")
    print("wrote", os.path.relpath(path))


def wait_for(condition, seconds=300):
    """Until the condition holds, or the time is up."""
    deadline = time.time() + seconds
    while not condition() and time.time() < deadline:
        time.sleep(0.2)


def screens():
    """The screenshots in the README: the live tab, requests and statistics, all from one run."""
    random.seed(7)
    with tempfile.TemporaryDirectory() as tmp:
        server, record, stop = start(os.path.join(tmp, "bridge.log"))
        time.sleep(60)
        router = server.router
        states = {"writing", "calling", "thinking", "waiting", "tool"}
        notes = ("daily limit", "window limit", "masked stream error", "upstream error", "client closed")
        parked = lambda: any(t > time.time() for t in router.parked.values())  # noqa: E731
        working = lambda: {a.get("state") for a in router.visible_agents() if a["active"]}  # noqa: E731
        full = lambda: CAP in [[router.sessions.get(a["session"], [None])[0] for a in router.visible_agents()  # noqa: E731
                               if a["active"]].count(i) for i in range(len(router.keys))]
        wait_for(lambda: parked() and full() and states <= working())
        save(server, record, "live")  # every agent state, the parked key, a key at its active cap
        wait_for(lambda: all(n in " | ".join(e["note"] for e in list(router.events)[-44:]) for n in notes))
        save(server, record, "requests", tab=1)  # every outcome
        save(server, record, "stats", tab=2)
        stop()


def main():
    parser = argparse.ArgumentParser(description="The panel with a fake upstream and fake agents.")
    parser.add_argument("--lang", choices=("en", "tr"), default="en")
    parser.add_argument("--screens", action="store_true", help="write the README screenshots and exit")
    args = parser.parse_args()
    if args.screens:
        return screens()
    with tempfile.TemporaryDirectory() as tmp:
        server, record, stop = start(os.path.join(tmp, "bridge.log"))
        try:
            panel.run(server, record, args.lang)
        except KeyboardInterrupt:
            pass
        stop()


if __name__ == "__main__":
    main()
