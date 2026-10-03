"""evren-bridge: a small local proxy for OpenAI Chat Completions clients that spreads requests over several API keys
of one upstream and keeps each session on one key.

Rules (routing D-01):
- Each session sticks to one key: a provider's prompt cache may be per account, so a switch can cost a full-price turn.
  The session id is the first non-empty X-Session-Affinity, X-Session-Id or Agent-Session-Id header (Pi sends the
  first). A request without one is placed and counted while it runs, then forgotten: no home, no return.
- A session is active while a request is in flight or while the client runs a tool it asked for (at most 10 minutes
  per tool call); once an answer ends the turn it is idle: finished, or waiting for its lead.
- A new session goes to the first key, in keys.txt order, with fewer than --active-cap (20) active sessions; if none
  qualifies, to the free key with the fewest active ones. Idle sessions take no room. Load never moves a session.
  The bridge remembers, and the panel shows, each session for 60 minutes after its last request; a session that
  comes back returns to its key (where its cache may still be), over the cap if need be, unless the key is parked.
  Such a return is logged with its idle gap (back=seconds).
- Requests go to --upstream (a root URL; its path, if any, is put before each request path) with the client's
  headers, except hop-by-hop ones, Authorization (the chosen key replaces it), Accept-Encoding and the session ids.
- Without a profile, answers pass unchanged: 429 and 503 with their bodies and Retry-After, streams as they come.
  Only a profile (profiles/evren.py, --profile evren) may park a key, retry on another key, add Retry-After, turn a
  masked stream error into a 503 or read quota numbers.
- Keys come only from ~/.evren/keys.txt (one "label=key" per line) and are never logged.
- The panel follows each session's current request: waiting for the first token, thinking, writing, writing a tool
  call; between requests the tool the client runs (name only, from the stream) or idle. Tool arguments are never kept.
- The upstream's model list (GET /v1/models, once a minute) is shown in the panel only; a model missing from it is
  "not listed", which says nothing about its health.
- GET /bridge/quota answers locally: one line per key with its bridge state, plus the profile's quota numbers.
  Keys never appear in it.
- The log keeps time, key label, path (no query), model, status, token counts, seconds, first-token time, tokens per
  second and session; never message content. The panel's statistics tab reads its history from this log.

Run:  python evren_bridge.py [--upstream URL] [--profile evren|none] [--active-cap 20] [--lang tr|en] [--no-panel]
      (listens on 127.0.0.1:8787; without --upstream the EVREN profile and its URL)
"""
import argparse
import atexit
import collections
import datetime
import itertools
import http.client
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from profiles import evren

UPSTREAM = os.environ.get("EVREN_BRIDGE_UPSTREAM")
KEYS_FILE = os.environ.get("EVREN_KEYS_FILE", os.path.expanduser("~/.evren/keys.txt"))
PORT = int(os.environ.get("EVREN_BRIDGE_PORT", "8787"))
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "bridge.log")
HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "proxy-connection", "te",
              "trailer", "transfer-encoding", "upgrade"}
HOP_HEADERS = HOP_BY_HOP | {"content-length", "host"}
TOOL_TIMEOUT = 600  # a tool running longer than this counts as idle, so a crashed client does not hold a slot
HOME_MEMORY = 3600  # a session's key, and its row in the panel, are kept this long after its last request
IN_FLIGHT = {"waiting", "thinking", "writing", "calling"}
SESSION_HEADERS = ("X-Session-Affinity", "X-Session-Id", "Agent-Session-Id")
# Not sent upstream: the key replaces Authorization, and the bridge reads usage from plain bodies (no gzip).
LOCAL_HEADERS = HOP_HEADERS | {"authorization", "accept-encoding"} | {name.lower() for name in SESSION_HEADERS}
QUOTA_EVERY = 60
ECHO = True  # print log lines; the panel turns this off


def session_of(headers):
    """The client's session id, or None when it sends none."""
    for name in SESSION_HEADERS:
        value = (headers.get(name) or "").strip()
        if value:
            return value
    return None


def load_keys(path):
    keys = []
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            label, _, key = line.partition("=")
            if key.strip():
                keys.append((label.strip(), key.strip()))
    if not keys:
        sys.exit(f"no keys in {path}")
    return keys


class Router:
    """Key choice per session, plus the live numbers the panel shows."""

    def __init__(self, keys, active_cap=20):
        self.keys = keys
        self.active_cap = active_cap
        self.started = time.time()
        self.parked = {}  # key index -> unix time when the daily limit resets
        self.sessions = {}  # session id -> [key index, last seen]
        self.minute = [collections.deque() for _ in keys]  # per key: finish times of the last minute's requests
        self.remaining = [collections.deque() for _ in keys]  # per key: (time, daily remaining tokens), with a profile
        self.in_flight = [0] * len(keys)
        self.flying = collections.Counter()  # session id -> its requests in flight
        self.latest = {}  # session id -> ticket of its newest request
        self.tickets = 0
        self.events = collections.deque(maxlen=2000)
        self.event_count = 0  # events ever added, so readers can tell which ones are new
        self.listeners = []  # called with every event, e.g. the statistics
        self.quota = {}  # key index -> the profile's latest quota numbers
        self.agents = {}  # session id -> what the panel shows about that agent
        self.passing = set()  # ids of requests that came without a session id; forgotten when they end
        self.listed = None  # model ids the upstream currently lists, from /v1/models
        self.lock = threading.Lock()

    def pick(self, session, tried=()):
        """The key for this session's next request; keys in `tried` already refused it today."""
        with self.lock:
            now = time.time()
            self._forget(now)
            if not tried:
                self._claim_return(session, now)
            free = [i for i in range(len(self.keys)) if self.parked.get(i, 0) <= now and i not in tried]
            if not free:
                return None
            held = self.sessions.get(session)
            if held and held[0] in free:
                held[1] = now
                return held[0]
            active, _ = self._session_counts(now)
            fits = [i for i in free if active[i] < self.active_cap]
            i = fits[0] if fits else min(free, key=lambda i: active[i])
            self.sessions[session] = [i, now]
            return i

    def park(self, i, until):
        with self.lock:
            self.parked[i] = max(until, time.time() + 60)  # a reset time in the past must not retry at once

    def earliest_reset(self):
        with self.lock:
            return min(self.parked.values())

    def passing_session(self):
        """A request-local id for a request without a session id: a tuple, so no header value can equal it."""
        with self.lock:
            self.tickets += 1
            session = ("-", self.tickets)
            self.passing.add(session)
            return session

    def release(self, session):
        """Forget a request-local session once its request has ended."""
        with self.lock:
            if session in self.passing:
                self.passing.discard(session)
                self.sessions.pop(session, None)
                self.agents.pop(session, None)
                self.latest.pop(session, None)

    def begin(self, i, session):
        """Count a request in flight and return its ticket."""
        with self.lock:
            self.in_flight[i] += 1
            self.flying[session] += 1
            self.tickets += 1
            self.latest[session] = self.tickets
            return self.tickets

    def end(self, i, session):
        with self.lock:
            self.in_flight[i] -= 1
            self.flying[session] -= 1
            if not self.flying[session]:
                del self.flying[session]

    def add(self, i, event):
        """Keep one finished request for the panel and, when it hit a key, for that key's requests per minute."""
        with self.lock:
            self.events.append(event)
            self.event_count += 1
            if i is not None:
                self.minute[i].append(event["time"])
                self._per_minute(i, event["time"])  # prune now: without the panel nothing else would
        for listener in self.listeners:
            listener(event)

    def update_quota(self, i, fields, now=None):
        with self.lock:
            now = now or time.time()
            if "daily_reset" in fields and fields["daily_reset"] != self.quota.get(i, {}).get("daily_reset"):
                self.remaining[i].clear()  # a new quota day: the old readings say nothing about this minute
            if fields.get("daily_remaining_tokens") is not None:
                self.remaining[i].append((now, fields["daily_remaining_tokens"]))
                self._tpm(i, now)  # prune
            self.quota[i] = {**self.quota.get(i, {}), **fields}

    def agent(self, session, ticket=None, **fields):
        """Update what the panel shows; a request that is no longer the session's newest changes nothing."""
        with self.lock:
            if ticket is not None and self.latest.get(session) != ticket:
                return
            agent = self.agents.setdefault(session, {})
            if fields.get("state", agent.get("state")) != agent.get("state"):
                agent["since"] = time.time()
            agent.update(fields)
            if session in self.sessions:
                self.sessions[session][1] = time.time()  # a long answer still counts as activity

    def visible_agents(self):
        """Agents the panel lists: every session the router remembers, each marked active or not."""
        with self.lock:
            now = time.time()
            self._forget(now)
            rows = []
            for sid, agent in self.agents.items():
                if sid in self.sessions:
                    rows.append({"session": "-" if sid in self.passing else sid, **agent,
                                 "active": self._active(sid, now)})
                    if agent.get("state") == "tool" and not rows[-1]["active"]:  # idle since its tool timed out
                        rows[-1].update(state="idle", since=agent["since"] + TOOL_TIMEOUT)
            return rows

    def pending_return(self, session):
        """Whole seconds this session had been idle before the request now under way, or None if it is no return."""
        with self.lock:
            return self.agents.get(session, {}).get("back")

    def _claim_return(self, session, now):
        """A request from a session whose turn had ended is a return. Its gap stays on the agent until an answer
        succeeds, so the client's retry after a 429, 503 or hang-up keeps the first gap. The claim marks the agent
        waiting at once, so a second request arriving with it does not count the same pause. After a tool that ran
        past TOOL_TIMEOUT the turn had not ended: no return."""
        agent = self.agents.get(session)
        if session not in self.sessions or not agent:
            return
        if agent.get("back") is None and agent.get("state") == "idle":
            agent["back"] = int(now - agent.get("since", now))
        if agent.get("back") is not None:
            agent["state"], agent["since"] = "waiting", now

    def status(self):
        with self.lock:
            now = time.time()
            self._forget(now)
            active, idle = self._session_counts(now)
            rows = []
            for i, (label, _) in enumerate(self.keys):
                rows.append({"label": label, "active": active[i], "idle": idle[i], "in_flight": self.in_flight[i],
                             "requests_per_min": self._per_minute(i, now), "tpm": self._tpm(i, now),
                             "parked_until": self.parked[i] if self.parked.get(i, 0) > now else None,
                             "quota": self.quota.get(i)})
            return rows

    def _forget(self, now):
        for sid, (_, seen) in list(self.sessions.items()):
            if now - seen > HOME_MEMORY:
                del self.sessions[sid]
                self.agents.pop(sid, None)
                self.latest.pop(sid, None)

    def _active(self, session, now):
        if self.flying[session]:
            return True  # another of its requests may already have recorded "idle"
        agent = self.agents.get(session)
        if not agent or agent.get("state") is None:
            return True  # placed a moment ago, its first request is on the way
        if agent["state"] in IN_FLIGHT:
            return True
        return agent["state"] == "tool" and now - agent.get("since", now) <= TOOL_TIMEOUT

    def _session_counts(self, now):
        """Active and idle sessions per key, among those the router remembers."""
        active, idle = [0] * len(self.keys), [0] * len(self.keys)
        for sid, (i, _) in self.sessions.items():
            if self._active(sid, now):
                active[i] += 1
            else:
                idle[i] += 1
        return active, idle

    def _per_minute(self, i, now):
        times = self.minute[i]
        while times and times[0] < now - 60:
            times.popleft()
        return len(times)

    def _tpm(self, i, now):
        """Tokens the profile counted on this key in the last minute: the newest reading a minute old minus now."""
        readings = self.remaining[i]
        while len(readings) > 1 and readings[1][0] <= now - 60:
            readings.popleft()
        if len(readings) < 2:
            return 0
        return max(0, readings[0][1] - readings[-1][1])


def as_dict(value):
    return value if isinstance(value, dict) else {}


def first_choice(text):
    """(chunk, first choice) of an OpenAI-style JSON body or stream chunk, or (None, None) for any other shape."""
    try:
        chunk = json.loads(text)
        choice = chunk["choices"][0]
    except (ValueError, KeyError, IndexError, TypeError):
        return None, None
    if not isinstance(chunk, dict) or not isinstance(choice, dict):
        return None, None
    return chunk, choice


class ClientGone(Exception):
    """The client closed its connection while the bridge was still answering."""


class Turn:
    """What one request's stream says about its agent: phase, tool name, first and last token times, finish reason."""

    def __init__(self, started):
        self.started = started
        self.state = "waiting"
        self.tool = None
        self.finish = None
        self.first = self.last = None

    def see(self, line):
        """Read one SSE data line (or a JSON body after "data:"); return True when the phase changed."""
        if not line.startswith(b"data:"):
            return False
        _, choice = first_choice(line[5:])
        if choice is None:
            return False
        part = as_dict(choice.get("delta") or choice.get("message"))
        calls = part.get("tool_calls")
        state = self.state
        if isinstance(calls, list) and calls:
            state = "calling"
            self.tool = as_dict(as_dict(calls[0]).get("function")).get("name") or self.tool
        elif part.get("content"):
            state = "writing"
        elif part.get("reasoning") or part.get("reasoning_content"):
            state = "thinking"
        output = (isinstance(calls, list) and calls) or part.get("content") or part.get("reasoning") \
            or part.get("reasoning_content")
        if output:  # finish, usage and empty chunks do not move the clock
            self.first = self.first or time.time()
            self.last = time.time()
        self.finish = choice.get("finish_reason") or self.finish
        changed, self.state = state != self.state, state
        return changed

    def ttft(self):
        return self.first - self.started if self.first else None

    def tps(self, usage):
        out = (usage or {}).get("out")
        if not out or not self.first or self.last - self.first < 0.5:
            return None
        return out / (self.last - self.first)


def usage_of(chunk):
    """Pick token counts out of a JSON body or one SSE data line."""
    text = chunk.decode("utf-8", "replace").strip()
    if text.startswith("data:"):
        text = text[5:].strip()
    try:
        usage = as_dict(as_dict(json.loads(text)).get("usage"))
    except ValueError:
        return None
    if not usage:
        return None
    details = as_dict(usage.get("prompt_tokens_details"))
    count = lambda value: value if isinstance(value, int) else None
    return {"prompt": count(usage.get("prompt_tokens")), "cached": count(details.get("cached_tokens")),
            "out": count(usage.get("completion_tokens"))}


def log(label, path, model, status, usage=None, note="", session="-", seconds=None, ttft=None, tps=None, back=None,
        profile="none"):
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"{stamp} {label} {path} model={model or '-'} status={status}"
    if usage:
        line += f" prompt={usage['prompt']} cached={usage['cached']} out={usage['out']}"
    if note:
        line += f" {note}"
    if seconds is not None:
        line += f" secs={seconds:.3f}"
    if ttft is not None:
        line += f" ttft={ttft:.2f}"
    if tps is not None:
        line += f" tps={tps:.0f}"
    if back is not None:
        line += f" back={back}"
    line += f" profile={profile} session={session[-8:]}"
    if ECHO:
        print(line, flush=True)
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def check_upstream(url):
    """The upstream URL without a trailing slash; ValueError if the bridge cannot use it."""
    parts = urlsplit(url)
    try:
        parts.port
    except ValueError:
        raise ValueError(f"bad port in upstream URL {url}") from None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"upstream URL must start with http:// or https:// and name a host: {url}")
    if "@" in parts.netloc or parts.query or parts.fragment:
        raise ValueError(f"upstream URL must not carry a user, query or fragment: {url}")
    return url.rstrip("/")


def target(upstream, path):
    """The upstream's own path prefix, if any, followed by the request path."""
    return urlsplit(upstream).path + path


def connect(upstream, timeout):
    url = urlsplit(upstream)
    cls = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
    return cls(url.netloc, timeout=timeout)


def fetch(upstream, key, path):
    """(status, body) of one GET to the upstream with this key."""
    conn = connect(upstream, 15)
    try:
        conn.request("GET", target(upstream, path), headers={"Authorization": f"Bearer {key}"})
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def key_quota(upstream, key, profile):
    """The profile's quota numbers for one key, or {"error": ...}."""
    try:
        status, data = fetch(upstream, key, profile.QUOTA_PATH)
        if status != 200:
            return {"error": f"HTTP {status}"}
        return profile.quota(data)
    except (OSError, http.client.HTTPException, ValueError, AttributeError) as e:
        return {"error": type(e).__name__}


def list_models(upstream, key):
    """Model ids the upstream lists right now, or None when the list cannot be read."""
    try:
        status, data = fetch(upstream, key, "/v1/models")
        return {m["id"] for m in json.loads(data)["data"]} if status == 200 else None
    except (OSError, http.client.HTTPException, ValueError, KeyError, TypeError):
        return None


def refresh(server):
    """For the panel: the model list and, with a profile, every key's quota (idle keys send no headers)."""
    router = server.router
    if server.profile:
        for i, (_, key) in enumerate(router.keys):
            router.update_quota(i, key_quota(server.upstream, key, server.profile))
    router.listed = list_models(server.upstream, router.keys[0][1])


def poll(server):
    while True:
        refresh(server)
        time.sleep(QUOTA_EVERY)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        if self.path.split("?")[0] == "/bridge/quota":
            return self.bridge_quota()
        self.forward()

    def do_POST(self):
        self.forward()

    def log_message(self, *args):  # keep the console to our own log lines
        pass

    def bridge_quota(self):
        rows = []
        for (_, key), row in zip(self.server.router.keys, self.server.router.status()):
            del row["quota"]
            if row["parked_until"]:
                row["parked_until"] = datetime.datetime.fromtimestamp(
                    row["parked_until"], datetime.timezone.utc).isoformat()
            else:
                del row["parked_until"]
            profile = self.server.profile
            rows.append({**row, **(key_quota(self.server.upstream, key, profile) if profile else {})})
        data = json.dumps({"keys": rows}, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def forward(self):
        router = self.server.router
        self.session = session_of(self.headers)
        self.anonymous = self.session is None
        if self.anonymous:
            self.session = router.passing_session()
        try:
            self.attempts(router)
        finally:
            router.release(self.session)

    def attempts(self, router):
        self.back = None  # seconds idle before this request, when it is a return
        self.sent = False  # whether the client already got a status line from us
        self.ticket = None
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            model = as_dict(json.loads(body)).get("model", "") if body else ""
        except ValueError:
            model = ""
        tried = set()
        while True:
            self.started = time.time()  # each attempt is timed on its own
            self.turn = Turn(self.started)
            i = router.pick(self.session, tried)
            if not tried:
                self.back = router.pending_return(self.session)
            if i is None:
                return self.all_parked(router)
            tried.add(i)
            self.ticket = router.begin(i, self.session)
            router.agent(self.session, self.ticket, key=router.keys[i][0], model=model, state="waiting", tool=None)
            conn = None
            try:
                conn, resp = self.upstream(router.keys[i][1], body)
                if resp.status == 429 and self.server.profile:
                    data = resp.read()
                    until = self.server.profile.park_until(data)
                    if until:
                        router.park(i, until)
                        reset = datetime.datetime.fromtimestamp(until, datetime.timezone.utc).strftime("%H:%MZ")
                        self.record(i, model, 429, note=f"daily limit, parked until {reset}")
                        continue
                    return self.relay(resp, i, model, data, note="window limit, passed to client")
                return self.relay(resp, i, model)
            except ClientGone:
                self.close_connection = True
                return self.record(i, model, 499, note="client closed the connection")
            except (OSError, http.client.HTTPException) as e:
                return self.upstream_failed(i, model, e)
            finally:
                if conn:
                    conn.close()
                router.end(i, self.session)

    def upstream_failed(self, i, model, error):
        """The upstream could not be reached, or its stream broke. The client gets a 502 if it has heard nothing yet."""
        self.close_connection = True
        if self.sent:
            return self.record(i, model, 502, note=f"upstream error, {type(error).__name__}")
        data = json.dumps({"error": {"message": f"evren-bridge: the upstream did not answer ({type(error).__name__}); "
                                                "please retry your request",
                                     "type": "api_error", "code": "bad_gateway"}}).encode()
        try:
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.send(data)
        except ClientGone:
            return self.record(i, model, 499, note="client closed the connection")
        self.record(i, model, 502, note=f"upstream error, {type(error).__name__}")

    def record(self, i, model, status, usage=None, note=""):
        router = self.server.router
        label = router.keys[i][0] if i is not None else "-"
        seconds, ttft, tps = time.time() - self.started, self.turn.ttft(), self.turn.tps(usage)
        session = "-" if self.anonymous else self.session
        profile = self.server.profile.NAME.lower() if self.server.profile else "none"
        log(label, self.path.split("?")[0], model, status, usage, note, session, seconds, ttft, tps, self.back, profile)
        router.add(i, {"time": time.time(), "key": label, "session": session, "model": model, "status": status,
                       "usage": usage, "note": note, "seconds": seconds, "ttft": ttft, "tps": tps, "back": self.back,
                       "profile": profile})
        if status == 499:  # the client hung up; the agent is not running any more
            router.agent(self.session, self.ticket, state="idle")
            return
        if status >= 400:
            router.agent(self.session, self.ticket, state="error", status=status)
            return
        usage = usage or {}
        prompt, cached = usage.get("prompt"), usage.get("cached")
        router.agent(self.session, self.ticket, state="tool" if self.turn.finish == "tool_calls" else "idle",
                     tool=self.turn.tool, tps=tps, ttft=ttft, context=prompt, back=None,
                     cache=cached / prompt if prompt and cached is not None else None)

    def watch(self, line):
        if self.turn.see(line):
            self.server.router.agent(self.session, self.ticket, state=self.turn.state, tool=self.turn.tool)

    def upstream(self, key, body):
        conn = connect(self.server.upstream, 900)
        named = {name.strip().lower() for name in ",".join(self.headers.get_all("Connection", [])).split(",")}
        headers = {name: value for name, value in self.headers.items()
                   if name.lower() not in LOCAL_HEADERS and name.lower() not in named}
        headers["Authorization"] = f"Bearer {key}"
        if "content-type" not in {name.lower() for name in headers}:
            headers["Content-Type"] = "application/json"
        try:
            conn.request(self.command, target(self.server.upstream, self.path), body=body or None, headers=headers)
            return conn, conn.getresponse()
        except BaseException:
            conn.close()
            raise

    def end_headers(self):
        """The first byte to the client leaves here, so this is where it counts as answered."""
        self.sent = True
        try:
            super().end_headers()
        except OSError as e:
            raise ClientGone from e

    def send(self, data):
        """Write to the client; a dropped connection on its side becomes ClientGone."""
        try:
            self.wfile.write(data)
            self.wfile.flush()
        except OSError as e:
            raise ClientGone from e

    def relay(self, resp, i, model, data=None, note=""):
        """Pass the upstream's answer to the client and record it once it is delivered."""
        profile = self.server.profile
        fields = profile.quota_from_headers(resp) if profile else {}
        if fields:
            self.server.router.update_quota(i, fields)
        is_json = data is not None or "json" in (resp.getheader("Content-Type") or "")
        held = []
        if is_json and data is None:
            data = resp.read()  # before any byte to the client, so a broken body still gets it a 502
        if not is_json and profile:
            # Hold back stream lines until the first data line: a masked 503 must become
            # a real status before anything reaches the client.
            while True:
                line = resp.readline()
                held.append(line)
                if not line or line.startswith(b"data:"):
                    break
            if profile.masked_error(held[-1]):
                self.unavailable()
                return self.record(i, model, 503, note="masked stream error, sent as 503 for retry")
        self.send_response(resp.status)
        named = {name.strip().lower() for name in (resp.getheader("Connection") or "").split(",")}
        for name, value in resp.getheaders():
            if name.lower() not in HOP_HEADERS and name.lower() not in named:
                self.send_header(name, value)
        if resp.status == 429 and profile and not resp.getheader("Retry-After"):
            self.send_header("Retry-After", profile.RETRY_AFTER)
        if is_json:
            if resp.status == 200:
                self.turn.see(b"data:" + data)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.send(data)
            return self.record(i, model, resp.status, usage_of(data), note)
        # SSE or other streams: copy line by line until the upstream closes
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        usage = None
        rest = iter(resp.readline, b"") if not held or held[-1] else ()
        for line in itertools.chain(held, rest):
            self.send(line)
            self.watch(line)
            if b'"usage"' in line:
                usage = usage_of(line) or usage
        self.record(i, model, resp.status, usage, note)

    def unavailable(self):
        data = json.dumps({"error": {"message": f"evren-bridge: {self.server.profile.NAME} could not reach the model "
                                                "(503 service unavailable, "
                                                "masked as finish_reason error); please retry your request",
                                     "type": "api_error", "code": "service_unavailable"}}).encode()
        self.send_response(503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.send(data)

    def all_parked(self, router):
        wait = max(1, int(router.earliest_reset() - time.time()))
        data = json.dumps({"error": {"message": f"evren-bridge: every key hit the daily limit; next reset in {wait}s",
                                     "type": "rate_limit_error", "code": "daily_token_limit_exceeded"}}).encode()
        try:
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("Retry-After", str(wait))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.send(data)
        except ClientGone:
            self.close_connection = True
            return self.record(None, "", 499, note="client closed the connection")
        self.record(None, "", 429, note="all keys parked")


def make_server(keys, upstream, port=PORT, active_cap=20, profile=None):
    upstream = check_upstream(upstream)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.router = Router(keys, active_cap)
    server.upstream = upstream
    server.profile = profile
    return server


def name_window(title="evren-bridge"):
    """Show the bridge's name in the terminal's title bar or tab instead of the shell's, and give the old one back."""
    if os.name == "nt":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        old = ctypes.create_unicode_buffer(1024)
        if kernel32.GetConsoleTitleW(old, len(old)):
            kernel32.SetConsoleTitleW(title)
            atexit.register(kernel32.SetConsoleTitleW, old.value)
    elif sys.stdout.isatty():
        sys.stdout.write(f"[22;0t]0;{title}")  # save the title, then set ours
        sys.stdout.flush()
        atexit.register(lambda: (sys.stdout.write("[23;0t"), sys.stdout.flush()))


def main():
    global ECHO
    parser = argparse.ArgumentParser(description="Local Chat Completions proxy that keeps each session on one API key.")
    parser.add_argument("--upstream", default=UPSTREAM, metavar="URL",
                        help="upstream root URL, /v1/... is added to it "
                             "(env EVREN_BRIDGE_UPSTREAM; default: the profile's)")
    parser.add_argument("--profile", choices=("evren", "none"),
                        help="provider rules; default evren without --upstream, none with it")
    parser.add_argument("--active-cap", type=int, default=20, help="active sessions per key before new ones go on")
    parser.add_argument("--no-panel", action="store_true", help="print log lines instead of the live panel")
    parser.add_argument("--lang", choices=("en", "tr"), default="en", help="panel language; L switches it live")
    args = parser.parse_args()
    profile = evren if (args.profile or ("none" if args.upstream else "evren")) == "evren" else None
    upstream = args.upstream or (profile.DEFAULT_URL if profile else None)
    if not upstream:
        sys.exit("evren-bridge: --profile none needs --upstream")
    try:
        check_upstream(upstream)
    except ValueError as e:
        sys.exit(f"evren-bridge: {e}")
    keys = load_keys(KEYS_FILE)
    name_window()
    server = make_server(keys, upstream, active_cap=args.active_cap, profile=profile)
    if args.no_panel:
        print(f"evren-bridge on http://127.0.0.1:{server.server_address[1]}/v1 -> {upstream} "
              f"(profile {profile.NAME if profile else 'none'}) with keys: " + ", ".join(label for label, _ in keys),
              flush=True)
        server.serve_forever()
        return
    import panel  # needs rich; only the panel does
    import stats

    history = stats.Stats.load(LOG_FILE, profile)  # before serving, so no request is counted twice
    history.attach(server.router)

    ECHO = False
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=poll, args=(server,), daemon=True).start()
    try:
        panel.run(server, history, args.lang)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
