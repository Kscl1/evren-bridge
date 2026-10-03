"""Tests for evren_bridge against a fake EVREN upstream. Run: python -m unittest -v"""
import email.message
import http.client
import io
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import evren_bridge
from profiles import evren
import stats

DAILY = {"error": {"message": "Günlük token limitiniz aşıldı", "type": "rate_limit_error",
                   "code": "daily_token_limit_exceeded",
                   "evren": {"resets_at": "2099-01-01T00:00:00Z", "remaining_tokens": 10}}}
DAILY_PAST = {"error": {**DAILY["error"], "evren": {"resets_at": "2000-01-01T00:00:00Z"}}}
WINDOW = {"error": {"message": "dakikalık token limitiniz aşıldı", "type": "rate_limit_error",
                    "code": "rate_limit_exceeded"}}
UNAVAILABLE = {"error": {"message": "modele bağlanılamadı", "type": "api_error", "code": "service_unavailable"}}
OK = {"choices": [{"message": {"content": "secret answer"}}],
      "usage": {"prompt_tokens": 100, "completion_tokens": 5, "prompt_tokens_details": {"cached_tokens": 80}}}
QUOTA = {"used_tokens": 0, "cap": 2500000, "window_minutes": 5, "reset_at": "2026-10-02T16:08:00+00:00",
         "usage_ratio": 0.0, "level": "ok", "daily_used_tokens": 0, "daily_limit_tokens": 10000000,
         "daily_remaining_tokens": 10000000, "daily_reset_at": "2026-10-03T00:00:00Z"}

STREAMS = {
    "sse": (b": keep-alive\n\n", b'data: {"id":"c1","choices":[{"delta":{"content":"hi"}}]}\n\n',
            b'data: {"usage":{"prompt_tokens":7,"completion_tokens":1}}\n\n', b"data: [DONE]\n\n"),
    # What EVREN sent on 26 Sep when its gateway could not reach the model (non-stream: 503 "modele bağlanılamadı")
    "masked": (b'data: {"choices": [{"index": 0, "delta": {}, "finish_reason": "error"}]}\n\n', b"data: [DONE]\n\n"),
    "single": (b'data: {"id":"c4","choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}],'
               b'"usage":{"prompt_tokens":7,"completion_tokens":1}}\n\n', b"data: [DONE]\n\n"),
    "late_error": (b'data: {"id":"c2","choices":[{"delta":{"content":"par"}}]}\n\n',
                   b'data: {"choices": [{"index": 0, "delta": {}, "finish_reason": "error"}]}\n\n', b"data: [DONE]\n\n"),
    "tool": (b'data: {"id":"c3","choices":[{"delta":{"reasoning":"hmm"}}]}\n\n',
             b'data: {"id":"c3","choices":[{"delta":{"tool_calls":[{"index":0,"function":{"name":"exec_command",'
             b'"arguments":""}}]}}]}\n\n',
             b'data: {"id":"c3","choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{}"}}]},'
             b'"finish_reason":"tool_calls"}]}\n\n',
             b'data: {"id":"c3","choices":[],"usage":{"prompt_tokens":40,"completion_tokens":12,'
             b'"prompt_tokens_details":{"cached_tokens":30}}}\n\n', b"data: [DONE]\n\n"),
}


class FakeEvren(BaseHTTPRequestHandler):
    """Answers per key: script[key] is a list of kinds (daily, window, ok, or a STREAMS name) consumed in order."""
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        key = self.headers["Authorization"].removeprefix("Bearer ")
        self.server.seen.append(self.path)
        if key in self.server.script and self.path == "/v1/models":
            data, status = json.dumps({"data": [{"id": "glm-5.3"}, {"id": "mimo-v2.6-pro"}]}).encode(), 200
        elif key not in self.server.script or self.path != "/v1/quota":
            data, status = b"{}", 404
        else:
            data, status = json.dumps({**QUOTA, "used_tokens": 5 if key == "k-A" else 0, "held_cr": "0"}).encode(), 200
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        key = self.headers["Authorization"].removeprefix("Bearer ")
        self.server.seen.append(key)
        self.server.headers.append(self.headers)
        self.server.paths.append(self.path)
        kind = self.server.script[key].pop(0) if self.server.script[key] else "ok"
        if kind == "cut":  # promises a body, sends one byte of it and hangs up
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "100")
            self.end_headers()
            self.wfile.write(b"{")
            self.close_connection = True
            return
        if kind in STREAMS:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            for line in STREAMS[kind]:
                self.wfile.write(line)
                self.wfile.flush()
            self.close_connection = True
            return
        payload = {"daily": DAILY, "daily_past": DAILY_PAST, "window": WINDOW, "window_bare": WINDOW, "ok": OK,
                   "hop": OK, "unavailable": UNAVAILABLE}[kind]
        data = json.dumps(payload).encode()
        self.send_response({"ok": 200, "hop": 200, "unavailable": 503}.get(kind, 429))
        if kind == "hop":  # a header meant for the bridge alone
            self.send_header("Connection", "X-Upstream-Only")
            self.send_header("X-Upstream-Only", "internal")
        self.send_header("Content-Type", "application/json")
        self.send_header("X-Evren-Daily-Limit-Tokens", "10000000")
        self.send_header("X-Evren-Daily-Remaining-Tokens", "9000000")
        self.send_header("X-Evren-Daily-Reset", "1790985600")
        if kind == "window":
            self.send_header("Retry-After", "30")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def start(server):
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class Hungup(io.RawIOBase):
    def writable(self):
        return True

    def write(self, data):
        raise BrokenPipeError


class GonePi(evren_bridge.Handler):
    """A Pi that hangs up before the bridge writes its first byte."""

    def setup(self):
        super().setup()
        self.wfile = Hungup()


class RoutingTest(unittest.TestCase):
    def setUp(self):
        self.router = evren_bridge.Router([("a", "k-A"), ("b", "k-B")], active_cap=1)

    def counts(self):
        return [(r["active"], r["idle"]) for r in self.router.status()]

    def test_idle_session_gives_its_active_place_back(self):
        self.assertEqual([self.router.pick("s1"), self.router.pick("s2")], [0, 1])
        self.router.agent("s1", state="idle")  # its answer ended the turn
        self.assertEqual(self.counts(), [(0, 1), (1, 0)])
        self.assertEqual(self.router.pick("s3"), 0)  # the idle agent does not push s3 to another key

    def test_idle_sessions_take_no_room(self):
        self.router.active_cap = 20
        for n in range(50):  # fifty subagents that finished within the hour
            self.router.pick(f"done-{n}")
            self.router.agent(f"done-{n}", state="idle")
        self.assertEqual(self.router.pick("new"), 0)
        self.assertEqual(self.counts()[0], (1, 50))

    def test_a_tool_counts_as_active_for_ten_minutes(self):
        self.router.pick("s1")
        self.router.agent("s1", state="tool", tool="bash")
        self.assertEqual(self.counts()[0], (1, 0))
        self.router.agents["s1"]["since"] -= evren_bridge.TOOL_TIMEOUT + 1  # Pi went quiet mid-tool
        self.assertEqual(self.counts()[0], (0, 1))

    def test_returning_session_goes_home_within_the_hour(self):
        self.assertEqual([self.router.pick("s1"), self.router.pick("s2")], [0, 1])
        self.router.agent("s1", state="idle")
        self.assertEqual(self.router.pick("s3"), 0)  # s1's active place is free for a new session
        self.router.agents["s1"]["since"] -= 1800
        self.assertEqual(self.router.pick("s1"), 0)  # s1 comes back home after 30 minutes, over the active cap
        self.assertAlmostEqual(self.router.pending_return("s1"), 1800, delta=5)
        self.router.sessions["s1"][1] -= evren_bridge.HOME_MEMORY + 1
        self.router.status()
        self.assertNotIn("s1", self.router.sessions)  # forgotten after an hour; it would be placed like a new one

    def test_idle_agent_stays_in_the_panel_for_the_hour(self):
        self.router.pick("s1")
        self.router.agent("s1", state="idle")
        self.router.agents["s1"]["since"] -= 3000
        self.assertEqual([(a["session"], a["active"]) for a in self.router.visible_agents()], [("s1", False)])
        self.router.sessions["s1"][1] -= evren_bridge.HOME_MEMORY + 1
        self.assertEqual(self.router.visible_agents(), [])  # forgotten and gone from the panel together
        self.router.pick("s1")
        self.assertIsNone(self.router.pending_return("s1"))  # placed like a new session, no return

    def test_only_an_ended_turn_makes_a_return(self):
        self.router.pick("s1")
        self.assertIsNone(self.router.pending_return("s1"))  # placed, first request on the way
        self.router.agent("s1", state="tool", tool="bash")
        self.router.pick("s1")
        self.assertIsNone(self.router.pending_return("s1"))  # the next request after a tool is no return
        self.router.agent("s1", state="tool", tool="bash")
        self.router.agents["s1"]["since"] -= evren_bridge.TOOL_TIMEOUT + 60
        self.assertEqual([(a["state"], round(time.time() - a["since"])) for a in self.router.visible_agents()],
                         [("idle", 60)])  # shown idle from the moment its tool timed out
        self.router.pick("s1")
        self.assertIsNone(self.router.pending_return("s1"))  # but its turn had not ended
        self.router.agent("s1", state="idle")
        self.router.pick("s1")
        self.assertIsNotNone(self.router.pending_return("s1"))

    def test_two_requests_together_claim_one_return(self):
        self.router.pick("s1")
        self.router.agent("s1", state="idle")
        self.router.agents["s1"]["since"] -= 900
        self.router.pick("s1")
        self.router.agents["s1"]["back"] = None  # as if the first one's answer had already cleared it
        self.router.pick("s1")
        self.assertIsNone(self.router.pending_return("s1"))  # the claim made it waiting: no second return

    def test_tpm_is_evren_daily_remaining_a_minute_ago_minus_now(self):
        now, day = time.time(), 1_800_000_000
        for age, left in ((70, 9_000_000), (30, 8_900_000), (0, 8_700_000)):
            self.router.update_quota(0, {"daily_remaining_tokens": left, "daily_reset": day}, now=now - age)
        self.assertEqual(self.router._tpm(0, now), 300_000)
        self.router.update_quota(0, {"daily_remaining_tokens": 9_999_000, "daily_reset": day + 86400}, now=now)
        self.assertEqual(self.router._tpm(0, now), 0)  # a new EVREN day starts a fresh count

    def test_session_id_comes_from_the_first_of_three_headers(self):
        def headers(**pairs):
            message = email.message.Message()
            for name, value in pairs.items():
                message[name.replace("_", "-")] = value
            return message

        self.assertEqual(evren_bridge.session_of(headers(agent_session_id="c", x_session_id="b",
                                                         x_session_affinity="a")), "a")
        self.assertEqual(evren_bridge.session_of(headers(AGENT_SESSION_ID="c", X_SESSION_ID="b")), "b")
        self.assertEqual(evren_bridge.session_of(headers(x_session_affinity="  ", agent_session_id=" c ")), "c")
        self.assertIsNone(evren_bridge.session_of(headers(session_id="x")))

    def test_requests_without_a_session_id_are_placed_apart_and_forgotten(self):
        first, second = self.router.passing_session(), self.router.passing_session()
        self.assertNotEqual(first, second)
        self.assertEqual(self.router.pick(first), 0)
        self.router.begin(0, first)
        self.assertEqual(self.router.pick(second), 1)  # the first one is active on a, so not the same session
        self.router.begin(1, second)
        self.router.agent(first, state="writing")
        self.assertEqual([a["session"] for a in self.router.visible_agents()], ["-"])
        self.assertEqual(self.counts(), [(1, 0), (1, 0)])
        for i, session in enumerate((first, second)):
            self.router.agent(session, state="idle")
            self.router.end(i, session)
            self.router.release(session)
        self.assertEqual(self.counts(), [(0, 0), (0, 0)])  # no idle row, no home
        self.assertEqual((self.router.sessions, self.router.agents, self.router.visible_agents()), ({}, {}, []))

    def test_full_keys_send_a_new_session_to_the_fewest(self):
        for s in ("s1", "s2", "s3"):
            self.router.pick(s)
        self.assertEqual(self.router.sessions["s3"][0], 0)
        self.assertEqual(self.router.pick("s4"), 1)

    def test_panel_renders_keys_and_requests(self):
        from rich.console import Console
        import panel
        self.router.pick("s1")
        self.router.park(1, time.time() + 600)
        now = time.time()
        self.router.update_quota(0, {"daily_used_tokens": 5, "daily_limit_tokens": 10, "daily_remaining_tokens": 125_005,
                                   "daily_reset": 1}, now=now - 61)
        self.router.update_quota(0, {"daily_used_tokens": 5, "daily_limit_tokens": 10, "daily_remaining_tokens": 5,
                                   "daily_reset": 1}, now=now)
        self.router.add(0, {"time": time.time(), "key": "a", "session": "s1", "model": "glm-5.3", "status": 200,
                          "usage": {"prompt": 100, "cached": 80, "out": 5}, "note": "", "seconds": 1.5})
        self.router.listed = {"glm-5.3"}
        self.router.agent("s1", key="a", model="glm-5.3", state="tool", tool="exec_command", tps=41.0)
        console = Console(file=io.StringIO(), width=160, record=True)
        console.print(panel.render(self.router, 40, profile=evren))
        text = console.export_text()
        for part in ("EVREN", "1/1", "limitte", "125k", "5 kaldı", "▸ exec_command çalışıyor", "1.5s", "listelenmiyor",
                     "1 ajan"):
            self.assertIn(part, text)
        self.assertNotIn("kapalı", text)  # a model the list leaves out is not called down

    def test_model_colours_never_mean_good_fair_weak_or_error(self):
        import panel
        reserved = {panel.GREEN, panel.YELLOW, panel.ORANGE, panel.RED}
        colours = set(panel.PALETTE) | set(evren.MODELS.values())
        self.assertFalse(colours & reserved)
        self.assertFalse(colours & {colour for colour, _ in panel.STATES.values()})

    def test_statistics_say_when_the_models_table_is_cut(self):
        from rich.console import Console
        import panel
        history = stats.Stats()
        for n in range(10):
            history.take({"time": time.time() - n, "key": "a", "session": f"s{n}", "model": f"model-{n:02d}",
                          "status": 200, "usage": None, "note": "", "seconds": 1.0})
        view = panel.View()
        view.tab = 2
        console = Console(file=io.StringIO(), width=200, record=True)
        console.print(panel.render(self.router, 60, view=view, stats=history))
        self.assertIn(f"Modeller  {panel.MODEL_ROWS} / 10", console.export_text())

    def test_anonymous_requests_never_touch_a_session_named_like_them(self):
        self.router.active_cap = 1
        self.assertEqual(self.router.pick("-1"), 0)
        self.router.agent("-1", state="tool", tool="bash")
        anonymous = self.router.passing_session()
        self.assertEqual(self.router.pick(anonymous), 1)  # key a is full with the real "-1"
        self.router.release(anonymous)
        self.assertEqual(self.router.sessions["-1"][0], 0)
        self.assertIn("-1", self.router.agents)

    def test_panel_columns_line_up_at_their_full_width(self):
        from rich.console import Console
        import panel

        def check(case, height, profile):
            history = stats.Stats(profile)
            for record in list(self.router.events):
                history.take(record)
            for tab in range(3):
                view = panel.View()
                view.tab = tab
                console = Console(file=io.StringIO(), width=200, record=True)
                console.print(panel.render(self.router, height, view=view, stats=history, profile=profile))
                lines = console.export_text().splitlines()
                self.assertEqual(len({len(line) for line in lines}), 1, (case, tab))  # every box has the same width
                self.assertNotIn("…", "".join(lines), (case, tab))  # no cell had to be cut
                widths.add(len(lines[0]))

        widths = set()
        for profile in (evren, None):
            check("no agents", 40, profile)
        self.router.active_cap = 30
        for n in range(20):
            sid = f"01a0fda3-7c11-4a2e-9d55-1f2e3d4c{n:04d}"
            self.router.pick(sid)
            self.router.agent(sid, key="a", model="deepseek-v4.1-flash", state="tool", tool="exec_command")
        check("twenty agents", 60, evren)
        check("more agents than rows", 30, evren)
        for n, model in enumerate(("llama-4-scout", "kimi-k3", "gpt-oss-120b", "mistral-big", "qwen3-coder") * 3):
            sid = f"other-{n:02d}"
            self.router.pick(sid)
            self.router.agent(sid, key="b", model=model, state="writing")
            self.router.add(1, {"time": time.time(), "key": "b", "session": sid, "model": model, "status": 200,
                                "usage": {"prompt": 1000 * n, "cached": 10 * n, "out": n}, "note": "", "seconds": 2.0,
                                "ttft": 0.5, "tps": 50.0})
        self.router.listed = {"kimi-k3"}
        check("no profile, unknown models", 60, None)
        check("no profile, unknown models, few rows", 30, None)
        self.assertEqual(len(widths), 1)  # with or without a profile

    def test_agents_box_pages_active_and_idle_apart(self):
        from rich.console import Console
        import panel
        self.router.active_cap = 60
        for n in range(37):
            sid = f"agent-{n:02d}"
            self.router.pick(sid)
            self.router.agent(sid, key="a", model="glm-5.3", state="idle" if n < 20 else "writing")
            self.router.agents[sid]["since"] -= 60 * n if n < 20 else 0  # agent-00 finished last
        view = panel.View()

        def shown(height=60):
            console = Console(file=io.StringIO(), width=200, record=True)
            console.print(panel.render(self.router, height, view=view))
            text = console.export_text()
            return text, [w for w in text.split() if w.startswith("agent-")]

        text, names = shown()
        self.assertIn("Aktif 17", text)
        self.assertIn("Boşta 20", text)
        self.assertIn("sayfa 1/2", text)
        self.assertEqual(len(names), 15)
        self.assertTrue(all(int(n[-2:]) >= 20 for n in names))  # only the working ones
        view.key("down")
        text, names = shown()
        self.assertIn("sayfa 2/2", text)
        self.assertEqual(len(names), 2)
        view.key("\t")  # to Idle, back to page 1
        text, names = shown()
        self.assertEqual((view.idle_tab, view.page), (True, 0))
        self.assertEqual(names[:2], ["agent-00", "agent-01"])  # most recently finished first
        for _ in range(9):
            view.key("pgdn")
        shown()
        self.assertEqual(view.page, 1)  # clamped to the last page
        view.key("home")
        self.assertEqual(view.page, 0)
        heights = []
        for page in (0, 1):  # the last page keeps the box height
            view.page = page
            text, _ = shown()
            heights.append(len(text.splitlines()))
        self.assertEqual(heights[0], heights[1])
        view.tab = 1
        view.key("\t")
        self.assertTrue(view.idle_tab)  # Tab only works in the live tab

    def test_paused_request_list_stays_still_while_requests_arrive(self):
        import panel

        def request(n):
            self.router.add(0, {"time": time.time(), "key": "a", "session": f"s-{n:04d}", "model": "glm-5.3",
                              "status": 200, "usage": None, "note": "", "seconds": 1.0})

        def first_shown(view):
            from rich.console import Console
            console = Console(file=io.StringIO(), width=200, record=True)
            console.print(panel.render(self.router, 20, view=view))
            return [w for w in console.export_text().split() if w.startswith("s-")][0]

        for n in range(50):
            request(n)
        view = panel.View()
        view.tab = 1
        self.assertEqual(first_shown(view), "s-0049")  # newest first, following the stream
        view.key("down")
        view.key("down")
        self.assertEqual(first_shown(view), "s-0047")
        request(50)
        request(51)
        self.assertEqual(first_shown(view), "s-0047")  # paused: new requests do not move the view
        view.key("home")
        self.assertEqual(first_shown(view), "s-0051")

    def test_keys_switch_tabs_and_quit(self):
        import panel
        view = panel.View()
        view.key("left")
        self.assertEqual(view.tab, 2)  # wraps around
        view.key("right")
        view.key("2")
        self.assertEqual(view.tab, 1)
        view.key("q")
        self.assertTrue(view.quit)

    def test_stream_phases_and_tool_name(self):
        turn = evren_bridge.Turn(time.time())
        lines = STREAMS["tool"]
        self.assertTrue(turn.see(lines[0]))
        self.assertEqual(turn.state, "thinking")
        turn.see(lines[1])
        self.assertEqual((turn.state, turn.tool), ("calling", "exec_command"))
        turn.see(lines[2])
        self.assertEqual((turn.tool, turn.finish), ("exec_command", "tool_calls"))
        self.assertFalse(turn.see(lines[3]))  # the usage chunk has no choices
        self.assertFalse(turn.see(b": keep-alive\n"))


class StatsTest(unittest.TestCase):
    OLD = "2026-09-24T22:13:55Z ekip-1 /v1/chat/completions model=glm-5.3 status=200 prompt=1000 cached=900 out=50\n"
    NEW = ("2026-10-03T10:00:10Z ekip-2 /v1/chat/completions model=glm-5.3 status=200 prompt=1000 cached=None "
           "out=50 secs=10.0 ttft=0.40 tps=40 session=aaaa1111\n")

    def test_old_and_new_log_lines_parse(self):
        old, new = stats.parse(self.OLD), stats.parse(self.NEW)
        self.assertEqual((old["prompt"], old["cached"], old["secs"], old["session"]), (1000, 900, None, "-"))
        self.assertEqual((new["cached"], new["secs"], new["ttft"], new["tps"], new["session"]),
                         (None, 10.0, 0.4, 40.0, "aaaa1111"))
        self.assertIsNone(stats.parse("not a log line\n"))

    def test_requests_without_an_id_are_no_agents(self):
        lines = [self.NEW.replace("aaaa1111", "-"), self.NEW.replace("10:00:10", "10:00:12").replace("aaaa1111", "-"),
                 self.NEW.replace("10:00:14", "10:00:14")]
        history = stats.Stats()
        history.records = [stats.parse(line) for line in lines]
        s = history.summary(None, history.records[-1]["time"] + 1)
        self.assertEqual((s["peak_requests"], s["peak_agents"], s["agents"]), (3, 1, 1))

    def test_quota_estimates_only_cover_requests_made_under_the_profile(self):
        legacy = self.OLD  # written before profiles were logged: EVREN
        other = self.NEW.replace("session=", "profile=none session=").replace("cached=None", "cached=0")
        history = stats.Stats(evren)
        history.records = sorted((stats.parse(line) for line in (legacy, other)), key=lambda r: r["time"])
        s = history.summary(None, history.records[-1]["time"] + 1)
        self.assertAlmostEqual(s["tokens"]["quota"], 100 + 900 * 0.03 + 50)  # the other provider's 1050 left out

    def test_returns_are_counted_by_gap_with_their_cache(self):
        lines = [self.NEW.replace("session=", "back=120 session="),
                 self.NEW.replace("10:00:10", "10:01:00").replace("cached=None", "cached=500").replace("session=", "back=900 session="),
                 self.NEW.replace("10:00:10", "10:02:00").replace("session=", "back=3000 session=").replace("status=200", "status=429"),
                 self.NEW.replace("10:00:10", "10:03:00")]
        history = stats.Stats()
        history.records = [stats.parse(line) for line in lines]
        self.assertEqual(history.records[0]["back"], 120.0)
        back = history.summary(None, history.records[-1]["time"] + 1)["returns"]
        self.assertEqual((back["count"], back["gaps"]), (2, [1, 1, 0]))  # the refused one is not counted
        self.assertEqual((back["prompt"], back["cached"]), (2000, 500))

    def test_quota_cost_weighs_cached_input_at_three_percent(self):
        self.assertAlmostEqual(evren.quota_cost(stats.parse(self.OLD)), 100 + 900 * 0.03 + 50)

    def test_load_and_summarise_a_range(self):
        lines = [self.OLD, self.NEW,
                 self.NEW.replace("10:00:10", "10:00:12").replace("aaaa1111", "bbbb2222"),  # overlaps the first
                 self.NEW.replace("10:00:10", "10:05:00").replace("status=200", "status=429"),
                 "2026-10-03T10:06:00Z ekip-1 /v1/chat/completions model=glm-5.3 status=429 daily limit, "
                 "parked until 00:00Z session=cccc3333\n"]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "bridge.log")
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(reversed(lines))  # load sorts by time
            history = stats.Stats.load(path)
        now = stats.parse(lines[-1])["time"] + 60
        everything = history.summary(None, now)
        self.assertEqual((everything["requests"], everything["agents"]), (5, 3))
        self.assertEqual((everything["peak_requests"], everything["peak_agents"]), (2, 2))
        self.assertEqual(everything["status"][429], 2)
        glm = everything["models"]["glm-5.3"]
        self.assertEqual((glm["tps"], glm["prompt"], glm["out"]), ([40.0, 40.0, 40.0], 4000, 200))
        self.assertEqual(len(everything["keys"]["ekip-1"]["limit_days"]), 1)
        self.assertEqual(sum(everything["load"]), 5)
        last_hour = history.summary(3600, now)
        self.assertEqual(last_hour["requests"], 4)  # the 24 September line is out of range
        self.assertEqual(history.summary(600, now + 3600)["requests"], 0)
        self.assertEqual(stats.Stats.load(os.path.join("no", "such.log")).records, [])

    def test_attached_stats_see_every_request_even_past_the_event_queue(self):
        router = evren_bridge.Router([("a", "k")])
        history = stats.Stats()
        history.attach(router)
        event = {"time": time.time(), "key": "a", "session": "s1", "model": "glm-5.3", "status": 200,
                 "usage": {"prompt": 10, "cached": 0, "out": 5}, "note": "", "seconds": 2.0, "ttft": 0.5, "tps": 30.0}
        router.add(0, event)
        self.assertEqual(history.summary(600)["requests"], 1)
        for _ in range(2100):  # more than the panel's 2,000-event queue holds
            router.add(0, event)
        summary = history.summary(600)  # a fresh event drops the reused summary
        self.assertEqual(summary["requests"], 2101)
        self.assertEqual(summary["models"]["glm-5.3"]["ttft"][:2], [0.5, 0.5])

    def test_a_record_added_while_a_summary_is_made_is_not_hidden_by_the_cache(self):
        history = stats.Stats()
        event = {"time": time.time(), "key": "a", "session": "s1", "model": "glm-5.3", "status": 200,
                 "usage": None, "note": "", "seconds": 1.0}
        history.take(event)
        real = stats.peaks

        def peaks_while_a_request_ends(rows):
            history.take({**event, "session": "s2"})
            return real(rows)

        stats.peaks = peaks_while_a_request_ends
        try:
            self.assertEqual(history.summary(600)["requests"], 1)
        finally:
            stats.peaks = real
        self.assertEqual(history.summary(600)["requests"], 2)

    def test_long_requests_fit_the_duration_column(self):
        import panel
        w = panel.WORDS["tr"]
        self.assertEqual([panel.took(s, w) for s in (0.04, 99.94, 99.97, 141.2, 999.4, 1000, 9000)],
                         ["0.0s", "99.9s", "100s", "141s", "999s", "16dk", "2sa"])

    def test_summary_cache_keeps_slice_counts_apart(self):
        history = stats.Stats()
        self.assertEqual(len(history.summary(600, slices=24)["load"]), 24)
        self.assertEqual(len(history.summary(600, slices=12)["load"]), 12)

    def test_statistics_tab_marks_the_best_model_and_switches_language(self):
        from rich.console import Console
        import panel
        router = evren_bridge.Router([("ekip-1", "k")])
        history = stats.Stats()
        for model, tps, ttft in (("deepseek-v4.1-flash", 160.0, 0.4), ("glm-5.3", 30.0, 1.5)):
            history.records.append({"time": time.time(), "key": "ekip-1", "model": model, "status": 200,
                                  "prompt": 1000, "cached": 900, "out": 100, "secs": 3.0, "ttft": ttft, "tps": tps,
                                  "session": model[:8], "daily_limit": False})
        view = panel.View()
        view.tab = 2
        view.key("up")  # 24 hours -> 1 hour
        self.assertEqual(view.range, 1)
        console = Console(file=io.StringIO(), width=200, record=True)
        console.print(panel.render(router, 60, view=view, stats=history))
        marked = [seg.text.strip() for seg in console._record_buffer
                  if seg.style and seg.style.bgcolor and seg.style.bgcolor.name == "#1f5f3f" and seg.text.strip()]
        self.assertIn("160", marked)  # DeepSeek's average TPS is the best
        self.assertIn("0.4s", marked)  # and so is its first token
        self.assertNotIn("30", marked)  # GLM's TPS is not
        text = console.export_text()
        self.assertIn("Özet · son 1 saat", text)
        self.assertIn("1.1k", text)  # input 1000 + output 100 per model
        view.key("l")
        console = Console(file=io.StringIO(), width=200, record=True)
        console.print(panel.render(router, 60, view=view, stats=history))
        self.assertIn("Summary · last 1 hour", console.export_text())


class HardeningTest(unittest.TestCase):
    """Cases from the GPT-6.1 Sol review of 3 October 2026."""

    def test_parsers_ignore_valid_json_of_the_wrong_shape(self):
        self.assertIsNone(evren.park_until(b'{"error": "offline"}'))
        self.assertFalse(evren_bridge.Turn(time.time()).see(b'data: {"choices": [null]}'))
        self.assertIsNone(evren_bridge.usage_of(b'{"usage": "bad"}'))
        self.assertFalse(evren.masked_error(b'data: {"choices": [null]}'))
        self.assertEqual(evren_bridge.usage_of(b'{"usage": {"prompt_tokens": "x", "completion_tokens": 3}}'),
                         {"prompt": None, "cached": None, "out": 3})

    def test_finish_chunk_does_not_stretch_the_token_clock(self):
        turn = evren_bridge.Turn(time.time())
        turn.see(b'data: {"id":"c","choices":[{"delta":{"content":"hi"}}]}')
        last = turn.last
        time.sleep(0.02)
        turn.see(b'data: {"id":"c","choices":[{"delta":{"content":""},"finish_reason":"stop"}]}')
        self.assertEqual(turn.last, last)
        self.assertEqual(turn.finish, "stop")

    def test_a_session_stays_active_while_any_of_its_requests_runs(self):
        router = evren_bridge.Router([("a", "k")])
        router.pick("s")
        router.begin(0, "s")
        router.begin(0, "s")
        router.agent("s", state="idle")  # one of the two answers ended its turn
        router.end(0, "s")
        self.assertEqual([(r["active"], r["idle"]) for r in router.status()], [(1, 0)])
        router.end(0, "s")
        self.assertEqual([(r["active"], r["idle"]) for r in router.status()], [(0, 1)])

    def test_a_reset_time_in_the_past_still_parks_the_key_for_a_minute(self):
        router = evren_bridge.Router([("a", "k")])
        router.park(0, time.time() - 5)
        self.assertGreater(router.parked[0], time.time() + 55)
        self.assertIsNone(router.pick("s"))

    def test_a_late_record_of_an_older_request_leaves_the_newer_one_alone(self):
        router = evren_bridge.Router([("a", "k")])
        router.pick("s")
        first = router.begin(0, "s")
        second = router.begin(0, "s")  # Pi already sent its next request
        router.agent("s", second, state="tool", tool="exec_command")
        router.end(0, "s")
        router.agent("s", first, state="idle", tool=None)  # the first answer is recorded only now
        self.assertEqual((router.agents["s"]["state"], router.agents["s"]["tool"]), ("tool", "exec_command"))

    def test_minute_history_is_pruned_without_the_panel(self):
        router = evren_bridge.Router([("a", "k")])
        old = time.time() - 120
        for n in range(500):
            router.add(0, {"time": old + n * 0.01, "usage": None})
            router.update_quota(0, {"daily_remaining_tokens": 1000 - n, "daily_reset": 1}, now=old + n * 0.01)
        router.add(0, {"time": time.time(), "usage": None})
        router.update_quota(0, {"daily_remaining_tokens": 1, "daily_reset": 1})
        self.assertEqual(len(router.minute[0]), 1)
        self.assertLessEqual(len(router.remaining[0]), 2)

    def test_events_that_arrive_out_of_order_are_still_in_range(self):
        router = evren_bridge.Router([("a", "k")])
        history = stats.Stats()
        history.attach(router)
        now = time.time()
        for when in (now - 5, now - 20):  # the older request finished later
            router.add(0, {"time": when, "key": "a", "session": "s", "model": "glm-5.3", "status": 200,
                         "usage": None, "note": "", "seconds": 1.0})
        self.assertEqual(history.summary(15, now)["requests"], 1)
        self.assertEqual(history.summary(30, now)["requests"], 2)

    def test_requests_shorter_than_the_log_precision_count_as_running(self):
        record = stats.parse("2026-10-03T10:00:10Z a /v1/chat/completions model=glm-5.3 status=200 "
                                  "secs=0.000 session=aaaa1111\n")
        self.assertEqual(stats.peaks([record]), (1, 1))


class Served(unittest.TestCase):
    """A bridge with keys a and b in front of the fake upstream."""
    profile = evren

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        evren_bridge.LOG_FILE = os.path.join(self.tmp.name, "bridge.log")
        self.fake = start(ThreadingHTTPServer(("127.0.0.1", 0), FakeEvren))
        self.fake.seen, self.fake.headers, self.fake.paths, self.fake.script = [], [], [], {"k-A": [], "k-B": []}
        upstream = f"http://127.0.0.1:{self.fake.server_address[1]}"
        self.bridge = start(evren_bridge.make_server([("a", "k-A"), ("b", "k-B")], upstream, 0, profile=self.profile))
        self.url = f"http://127.0.0.1:{self.bridge.server_address[1]}/v1/chat/completions"

    def tearDown(self):
        self.bridge.shutdown()
        self.fake.shutdown()
        self.bridge.server_close()
        self.fake.server_close()
        self.tmp.cleanup()

    def post(self, session=None):
        headers = {"Content-Type": "application/json"}
        if session:
            headers["X-Session-Affinity"] = session
        req = urllib.request.Request(self.url, data=b'{"model":"glm-5.3","messages":[]}', headers=headers)
        before = self.bridge.router.event_count
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()
        finally:
            self.settle(before)

    def settle(self, before, server=None):
        """The bridge records an answer just after Pi has it; wait for that record."""
        router, deadline = (server or self.bridge).router, time.time() + 5
        while time.time() < deadline and (router.event_count == before or any(router.in_flight)):
            time.sleep(0.005)
        self.assertGreater(router.event_count, before)
        self.assertFalse(any(router.in_flight))


class BridgeTest(Served):

    def test_stays_on_first_key(self):
        for _ in range(3):
            self.assertEqual(self.post()[0], 200)
        self.assertEqual(self.fake.seen, ["k-A"] * 3)

    def test_daily_limit_moves_to_next_key_and_stays(self):
        self.fake.script["k-A"] = ["daily"]
        status, _, body = self.post()
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["usage"]["prompt_tokens"], 100)
        self.post()
        self.assertEqual(self.fake.seen, ["k-A", "k-B", "k-B"])

    def test_window_limit_is_passed_through_on_same_key(self):
        self.fake.script["k-A"] = ["window"]
        status, headers, body = self.post()
        self.assertEqual(status, 429)
        self.assertEqual(headers["Retry-After"], "30")
        self.assertEqual(json.loads(body), WINDOW)
        self.assertEqual(self.post()[0], 200)
        self.assertEqual(self.fake.seen, ["k-A", "k-A"])

    def test_window_limit_without_retry_after_gets_60s(self):
        self.fake.script["k-A"] = ["window_bare"]
        status, headers, _ = self.post()
        self.assertEqual(status, 429)
        self.assertEqual(headers["Retry-After"], "60")

    def test_headers_the_upstream_names_in_connection_stay_behind(self):
        self.fake.script["k-A"] = ["hop"]
        status, headers, _ = self.post()
        self.assertEqual(status, 200)
        self.assertIsNone(headers["X-Upstream-Only"])

    def test_sessions_fill_a_key_before_the_next_and_stay(self):
        self.bridge.router.active_cap = 2
        self.fake.script["k-A"] = ["tool", "tool"]  # s1 and s2 stay active, running a tool
        for session in ("s1", "s2", "s3", "s1", "s3"):
            self.assertEqual(self.post(session)[0], 200)
        self.assertEqual(self.fake.seen, ["k-A", "k-A", "k-B", "k-A", "k-B"])
        self.assertNotIn("X-Session-Affinity", self.fake.headers[0])  # session ids stay local

    def test_requests_without_a_session_id_leave_nothing_behind(self):
        self.post()
        self.post()
        self.assertEqual(self.fake.seen, ["k-A", "k-A"])
        self.assertEqual((self.bridge.router.sessions, self.bridge.router.agents), ({}, {}))
        self.assertEqual([e["back"] for e in self.bridge.router.events], [None, None])  # never a return
        with open(evren_bridge.LOG_FILE, encoding="utf-8") as f:
            self.assertTrue(all(line.endswith(" session=-") for line in f.read().splitlines()))

    def test_a_lower_session_header_does_not_change_the_session(self):
        for headers in ({"x-session-id": "s1"}, {"X-Session-Affinity": "s1", "Agent-Session-Id": "s2"}):
            req = urllib.request.Request(self.url, data=b'{"model":"glm-5.3"}', headers=headers)
            before = self.bridge.router.event_count
            urllib.request.urlopen(req, timeout=10).read()
            self.settle(before)
        self.assertEqual(list(self.bridge.router.sessions), ["s1"])

    def test_client_headers_go_upstream_except_local_ones(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.bridge.server_address[1], timeout=10)
        conn.request("POST", "/v1/chat/completions", body=b'{"model":"glm-5.3"}', headers={
            "Content-Type": "application/json", "OpenAI-Project": "proj_A", "X-Provider-Extra": "1",
            "Authorization": "Bearer client-placeholder", "X-Session-Id": "s1", "Agent-Session-Id": "s2",
            "Connection": "keep-alive, X-Hop", "X-Hop": "drop", "Proxy-Authorization": "Basic x",
            "Accept-Encoding": "gzip", "TE": "trailers"})
        before = self.bridge.router.event_count
        resp = conn.getresponse()
        self.assertEqual((resp.status, json.loads(resp.read())), (200, OK))
        self.settle(before)
        conn.close()
        sent = self.fake.headers[0]
        self.assertEqual((sent["OpenAI-Project"], sent["X-Provider-Extra"]), ("proj_A", "1"))
        self.assertEqual(sent["Authorization"], "Bearer k-A")
        self.assertEqual(sent.get_all("Authorization"), ["Bearer k-A"])
        for name in ("X-Session-Id", "Agent-Session-Id", "X-Hop", "Proxy-Authorization", "TE"):
            self.assertNotIn(name, sent)
        self.assertNotEqual(sent["Accept-Encoding"], "gzip")
        self.assertEqual(sent["Host"], f"127.0.0.1:{self.fake.server_address[1]}")
        self.assertEqual(list(self.bridge.router.sessions), ["s1"])

    def test_upstream_path_prefix_is_joined_and_the_query_is_not_logged(self):
        upstream = f"http://127.0.0.1:{self.fake.server_address[1]}/tenant/acme/"
        prefixed = start(evren_bridge.make_server([("a", "k-A")], upstream, 0))
        try:
            url = f"http://127.0.0.1:{prefixed.server_address[1]}/v1/chat/completions?api_key=SECRET"
            req = urllib.request.Request(url, data=b'{"model":"glm-5.3"}', headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10).read()
            self.settle(0, prefixed)
            self.assertEqual(evren_bridge.list_models(prefixed.upstream, "k-A"), None)  # /tenant/acme/v1/models
        finally:
            prefixed.shutdown()
            prefixed.server_close()
        self.assertEqual(self.fake.paths, ["/tenant/acme/v1/chat/completions?api_key=SECRET"])
        self.assertEqual(self.fake.seen[-1], "/tenant/acme/v1/models")
        with open(evren_bridge.LOG_FILE, encoding="utf-8") as f:
            log = f.read()
        self.assertIn(" a /v1/chat/completions model=glm-5.3 status=200", log)
        self.assertNotIn("SECRET", log)

    def test_unusable_upstream_urls_are_refused_at_startup(self):
        for url in ("ftp://example.test", "example.test:443", "https://", "https://user:pw@example.test",
                    "https://example.test/v1?key=x", "https://example.test/#top", "http://example.test:port"):
            with self.subTest(url), self.assertRaises(ValueError):
                evren_bridge.make_server([("a", "k-A")], url, 0)
        self.assertEqual(evren_bridge.check_upstream("https://example.test/tenant/"), "https://example.test/tenant")

    def test_a_return_is_logged_with_its_idle_gap(self):
        self.post("s1")
        self.post("s1")
        self.assertIsNone(self.bridge.router.events[0]["back"])  # a new session
        self.assertLess(self.bridge.router.events[-1]["back"], 5)  # the turn had ended: a return, moments later
        self.bridge.router.agents["s1"]["since"] -= 1800
        self.post("s1")
        self.assertAlmostEqual(self.bridge.router.events[-1]["back"], 1800, delta=5)
        with open(evren_bridge.LOG_FILE, encoding="utf-8") as f:
            lines = f.read().splitlines()
        self.assertRegex(lines[-1], r" back=1[78]\d\d profile=evren session=s1$")
        self.assertEqual(sum(" back=" in line for line in lines), 2)

    def test_a_retried_return_keeps_its_first_gap(self):
        for failure in ("window", "masked"):
            with self.subTest(failure):
                session = f"s-{failure}"
                self.post(session)
                self.bridge.router.agents[session]["since"] -= 900
                self.fake.script["k-A"] = [failure]
                self.assertIn(self.post(session)[0], (429, 503))
                self.assertEqual(self.post(session)[0], 200)  # Pi retries
                gaps = [(e["status"], e["back"]) for e in list(self.bridge.router.events)[-2:]]
                self.assertEqual([g[0] for g in gaps], [429 if failure == "window" else 503, 200])
                self.assertTrue(all(890 <= g[1] <= 910 for g in gaps), gaps)
                self.assertIsNone(self.bridge.router.pending_return(session))  # cleared by the answer

    def test_agent_runs_the_tool_after_a_tool_call(self):
        self.fake.script["k-A"] = ["tool"]
        self.post("s1")
        agent = self.bridge.router.agents["s1"]
        self.assertEqual((agent["state"], agent["tool"], agent["key"]), ("tool", "exec_command", "a"))
        self.assertEqual((agent["context"], agent["cache"]), (40, 0.75))
        self.assertEqual([a["session"] for a in self.bridge.router.visible_agents()], ["s1"])
        self.post("s1")
        self.assertEqual(self.bridge.router.agents["s1"]["state"], "idle")

    def test_model_list_shows_which_models_are_up(self):
        upstream = f"http://127.0.0.1:{self.fake.server_address[1]}"
        self.assertEqual(evren_bridge.list_models(upstream, "k-A"), {"glm-5.3", "mimo-v2.6-pro"})
        self.assertIsNone(evren_bridge.list_models(upstream, "unknown"))

    def test_evren_headers_update_the_daily_numbers(self):
        self.post()
        quota = self.bridge.router.quota[0]
        self.assertEqual((quota["daily_used_tokens"], quota["daily_remaining_tokens"]), (1_000_000, 9_000_000))

    def test_all_keys_parked(self):
        self.fake.script = {"k-A": ["daily"], "k-B": ["daily"]}
        status, headers, _ = self.post()
        self.assertEqual(status, 429)
        self.assertGreater(int(headers["Retry-After"]), 0)
        self.assertEqual(self.post()[0], 429)
        self.assertEqual(self.fake.seen, ["k-A", "k-B"])  # parked keys are not retried

    def test_sse_passes_unchanged(self):
        self.fake.script["k-A"] = ["sse"]
        status, _, body = self.post()
        self.assertEqual(status, 200)
        self.assertEqual(body, b"".join(STREAMS["sse"]))

    def test_masked_stream_error_becomes_retryable_503_on_same_key(self):
        self.fake.script["k-A"] = ["masked"]
        status, _, body = self.post()
        self.assertEqual(status, 503)
        error = json.loads(body)["error"]
        self.assertIn("please retry your request", error["message"])  # a phrase Pi retries on
        self.assertEqual(error["code"], "service_unavailable")
        self.assertEqual(self.post()[0], 200)
        self.assertEqual(self.fake.seen, ["k-A", "k-A"])  # not parked, no key switch
        with open(evren_bridge.LOG_FILE, encoding="utf-8") as f:
            self.assertIn("status=503 masked stream error", f.read())

    def test_stream_error_after_output_passes_unchanged(self):
        self.fake.script["k-A"] = ["late_error"]
        status, _, body = self.post()
        self.assertEqual(status, 200)
        self.assertEqual(body, b"".join(STREAMS["late_error"]))

    def test_log_has_labels_and_counts_but_no_keys_or_content(self):
        self.fake.script["k-A"] = ["daily"]
        self.post()
        with open(evren_bridge.LOG_FILE, encoding="utf-8") as f:
            log = f.read()
        self.assertIn("a /v1/chat/completions model=glm-5.3 status=429 daily limit", log)
        self.assertIn("b /v1/chat/completions model=glm-5.3 status=200 prompt=100 cached=80 out=5 secs=", log)
        self.assertNotIn("k-A", log)
        self.assertNotIn("secret answer", log)

    def test_bridge_quota_lists_each_key_without_exposing_it(self):
        self.fake.script["k-A"] = ["daily"]
        self.post("s1")  # parks a, moves the session to b
        url = self.url.replace("/v1/chat/completions", "/bridge/quota")
        with urllib.request.urlopen(url, timeout=10) as r:
            raw = r.read()
        rows = json.loads(raw)["keys"]
        self.assertEqual([r["label"] for r in rows], ["a", "b"])
        self.assertEqual([r["active"] + r["idle"] for r in rows], [0, 1])
        self.assertEqual(rows[0]["used_tokens"], 5)
        self.assertEqual(rows[1]["daily_remaining_tokens"], 10000000)
        self.assertIn("parked_until", rows[0])
        self.assertNotIn("parked_until", rows[1])
        self.assertNotIn("held_cr", rows[0])  # only the fields we chose
        self.assertNotIn(b"k-A", raw)
        self.assertNotIn(b"k-B", raw)

    def test_bridge_quota_reports_a_key_error_per_key(self):
        self.fake.script = {"k-A": []}  # fake answers 404 for k-B
        url = self.url.replace("/v1/chat/completions", "/bridge/quota")
        with urllib.request.urlopen(url, timeout=10) as r:
            rows = json.loads(r.read())["keys"]
        self.assertEqual(rows[0]["used_tokens"], 5)
        self.assertEqual(rows[1]["error"], "HTTP 404")

    def test_unreachable_evren_gives_pi_a_502_and_frees_the_agent(self):
        down = start(evren_bridge.make_server([("a", "k-A")], "http://127.0.0.1:9", 0))  # nothing listens on port 9
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{down.server_address[1]}/v1/chat/completions",
                                         data=b'{"model":"glm-5.3"}', headers={"X-Session-Affinity": "s1"})
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(req, timeout=10)
            self.settle(0, down)
            self.assertEqual(caught.exception.code, 502)
            self.assertEqual(json.loads(caught.exception.read())["error"]["code"], "bad_gateway")
            self.assertEqual(down.router.agents["s1"]["state"], "error")
            self.assertEqual([(r["active"], r["idle"], r["in_flight"]) for r in down.router.status()], [(0, 1, 0)])
        finally:
            down.shutdown()
            down.server_close()

    def test_a_body_cut_by_evren_gives_pi_a_502(self):
        self.fake.script["k-A"] = ["cut"]
        status, _, body = self.post("s1")
        self.assertEqual(status, 502)
        self.assertEqual(json.loads(body)["error"]["code"], "bad_gateway")
        self.assertEqual([e["status"] for e in self.bridge.router.events], [502])

    def test_pi_hanging_up_is_recorded_once_as_499(self):
        self.bridge.RequestHandlerClass = GonePi
        for kind in ("ok", "sse", "masked", "window"):
            with self.subTest(kind):
                self.bridge.router.events.clear()
                self.fake.script["k-A"] = [kind]
                before = self.bridge.router.event_count
                with self.assertRaises((urllib.error.URLError, ConnectionError, http.client.HTTPException)):
                    urllib.request.urlopen(urllib.request.Request(
                        self.url, data=b'{"model":"glm-5.3"}', headers={"X-Session-Affinity": "s1"}), timeout=10)
                self.settle(before)
                self.assertEqual([e["status"] for e in self.bridge.router.events], [499])
                self.assertEqual(self.bridge.router.agents["s1"]["state"], "idle")
                self.assertEqual(self.bridge.router.status()[0]["in_flight"], 0)

    def test_pi_hanging_up_on_the_bridges_own_answers_is_a_499(self):
        self.bridge.RequestHandlerClass = GonePi
        cases = (("unreachable EVREN", "http://127.0.0.1:9", [], [499]),
                 ("all keys parked", None, ["daily"], [429, 429, 499]))
        for name, upstream, script, statuses in cases:
            with self.subTest(name):
                self.bridge.router.events.clear()
                if upstream:
                    self.bridge.upstream, saved = upstream, self.bridge.upstream
                self.fake.script = {"k-A": list(script), "k-B": list(script)}
                before = self.bridge.router.event_count
                with self.assertRaises((urllib.error.URLError, ConnectionError, http.client.HTTPException)):
                    urllib.request.urlopen(urllib.request.Request(
                        self.url, data=b'{"model":"glm-5.3"}', headers={"X-Session-Affinity": "s1"}), timeout=10)
                self.settle(before)
                if upstream:
                    self.bridge.upstream = saved
                self.assertEqual([e["status"] for e in self.bridge.router.events], statuses)

    def test_past_reset_times_try_each_key_once_then_give_pi_a_429(self):
        self.fake.script = {"k-A": ["daily_past"], "k-B": ["daily_past"]}
        status, headers, _ = self.post()
        self.assertEqual(status, 429)
        self.assertGreater(int(headers["Retry-After"]), 0)
        self.assertEqual(self.fake.seen, ["k-A", "k-B"])

    def test_usage_on_the_first_stream_line_is_counted(self):
        self.fake.script["k-A"] = ["single"]
        status, _, body = self.post()
        self.assertEqual(body, b"".join(STREAMS["single"]))
        with open(evren_bridge.LOG_FILE, encoding="utf-8") as f:
            self.assertIn("prompt=7 cached=None out=1", f.read())

    def test_listens_on_localhost_only(self):
        self.assertEqual(self.bridge.server_address[0], "127.0.0.1")


class NoProfileTest(Served):
    """--profile none: the bridge routes and counts, and passes every answer on as it came."""
    profile = None

    def test_a_429_without_retry_after_stays_without(self):
        self.fake.script["k-A"] = ["window_bare"]
        status, headers, body = self.post("s1")
        self.assertEqual((status, headers["Retry-After"], json.loads(body)), (429, None, WINDOW))

    def test_a_daily_limit_body_is_relayed_not_parked(self):
        self.fake.script["k-A"] = ["daily", "daily"]
        status, _, body = self.post("s1")
        self.assertEqual((status, json.loads(body)), (429, DAILY))
        self.assertEqual(self.post("s1")[0], 429)
        self.assertEqual(self.fake.seen, ["k-A", "k-A"])  # no retry on b, a not parked
        self.assertEqual(self.bridge.router.parked, {})

    def test_a_503_passes_unchanged(self):
        self.fake.script["k-A"] = ["unavailable"]
        status, _, body = self.post("s1")
        self.assertEqual((status, json.loads(body)), (503, UNAVAILABLE))
        self.assertEqual(self.fake.seen, ["k-A"])

    def test_a_masked_chunk_is_not_rewritten(self):
        self.fake.script["k-A"] = ["masked"]
        status, _, body = self.post("s1")
        self.assertEqual((status, body), (200, b"".join(STREAMS["masked"])))

    def test_streams_and_usage_still_count(self):
        self.fake.script["k-A"] = ["tool"]
        self.post("s1")
        agent = self.bridge.router.agents["s1"]
        self.assertEqual((agent["state"], agent["tool"], agent["context"]), ("tool", "exec_command", 40))

    def test_no_quota_is_read(self):
        self.post("s1")
        self.assertEqual(self.bridge.router.quota, {})  # the X-Evren headers are not read
        url = self.url.replace("/v1/chat/completions", "/bridge/quota")
        with urllib.request.urlopen(url, timeout=10) as r:
            rows = json.loads(r.read())["keys"]
        self.assertEqual([(r["label"], r["active"] + r["idle"]) for r in rows], [("a", 1), ("b", 0)])
        self.assertNotIn("used_tokens", rows[0])
        evren_bridge.refresh(self.bridge)  # what the panel's poller does once a minute
        self.assertEqual(self.bridge.router.listed, {"glm-5.3", "mimo-v2.6-pro"})  # the model list stays
        self.assertNotIn("/v1/quota", self.fake.seen)
        self.bridge.profile = evren
        evren_bridge.refresh(self.bridge)
        self.assertEqual(self.fake.seen.count("/v1/quota"), 2)  # one per key, with the profile only


if __name__ == "__main__":
    unittest.main()
