"""Statistics for the evren-bridge panel: every request from logs/bridge.log plus the bridge's live events, summarised
over a chosen time range."""
import bisect
import collections
import datetime
import re
import threading
import time

HEAD = re.compile(r"(\S+) (\S+) \S+ model=(\S+) status=(\d+)")
FIELD = re.compile(r"(\w+)=(\S+)")
FRESH = 2  # seconds a summary is reused; the panel redraws eight times a second
GAPS = (300, 1800)  # return gaps are counted under 5, 5-30 and 30-60 minutes


def parse(line):
    """One bridge.log line as a request record, or None. Old lines lack secs, ttft, tps, back and session."""
    head = HEAD.match(line)
    if not head:
        return None
    try:
        when = datetime.datetime.fromisoformat(head[1].replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
    fields = dict(FIELD.findall(line[head.end():]))

    def number(name, cast=float):
        try:
            return cast(fields[name])
        except (KeyError, ValueError):
            return None

    return {"time": when, "key": head[2], "model": head[3], "status": int(head[4]),
            "prompt": number("prompt", int), "cached": number("cached", int), "out": number("out", int),
            "secs": number("secs"), "ttft": number("ttft"), "tps": number("tps"), "back": number("back"),
            "session": fields.get("session", "-"), "daily_limit": "daily limit" in line,
            "profile": fields.get("profile", "evren")}  # lines from before profiles were logged came from EVREN


def percentile(values, share):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[round(share * (len(ordered) - 1))]


def peaks(records):
    """Most requests, and most distinct sessions, in flight at the same moment (records that carry a duration)."""
    edges = []
    for r in records:
        if r["secs"] is not None:  # a request shorter than the log's precision still ran
            start = r["time"] - r["secs"]
            end = max(r["time"], start + 1e-6)  # a request shorter than the log's precision still ends after it starts
            edges += [(start, 1, r["session"]), (end, -1, r["session"])]  # "-": a request without an id
    edges.sort(key=lambda edge: (edge[0], edge[1]))  # an end before a start at the same instant
    flying, in_flight, most_requests, most_sessions = collections.Counter(), 0, 0, 0
    for _, step, session in edges:
        in_flight += step
        if session not in ("-", None):
            flying[session] += step
            if not flying[session]:
                del flying[session]
        most_requests = max(most_requests, in_flight)
        most_sessions = max(most_sessions, len(flying))
    return most_requests, most_sessions


class Stats:
    def __init__(self, profile=None):
        self.profile = profile  # gives the quota estimate and the daily limit; without one there are no quota numbers
        self.records = []  # oldest first
        self.cached = {}  # (range, slices) -> (made at, summary)
        self.lock = threading.Lock()  # handler threads add records while the panel reads them
        self.version = 0  # bumped by every new record, so a summary made meanwhile is not cached

    @classmethod
    def load(cls, path, profile=None):
        stats = cls(profile)
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                stats.records = [r for r in map(parse, f) if r]
        except FileNotFoundError:
            pass
        stats.records.sort(key=lambda r: r["time"])
        return stats

    def attach(self, router):
        """Receive every request the bridge records from now on, however long the panel looks elsewhere."""
        router.listeners.append(self.take)

    def take(self, event):
        usage = event["usage"] or {}
        record = {"time": event["time"], "key": event["key"], "model": event["model"], "status": event["status"],
                  "prompt": usage.get("prompt"), "cached": usage.get("cached"), "out": usage.get("out"),
                  "secs": event["seconds"], "ttft": event.get("ttft"), "tps": event.get("tps"), "back": event.get("back"),
                  "session": event["session"][-8:], "daily_limit": event["note"].startswith("daily limit"),
                  "profile": event.get("profile", "none")}
        with self.lock:
            bisect.insort(self.records, record, key=lambda r: r["time"])  # requests can finish out of order
            self.version += 1
            self.cached.clear()

    def summary(self, seconds=None, now=None, slices=24):
        """Everything the statistics tab shows for the last `seconds` (None: all time), in `slices` equal parts.
        Per key, slices hold the quota estimate with a profile and the request count without one."""
        profile = self.profile
        now = now or time.time()
        with self.lock:
            made, result = self.cached.get((seconds, slices), (0, None))
            if result and now - made < FRESH:
                return result
            version = self.version
            start = now - seconds if seconds else (self.records[0]["time"] if self.records else now)
            rows = self.records[bisect.bisect_left(self.records, start, key=lambda r: r["time"]):]
        step = max(1.0, (now - start) / slices)
        empty = {"requests": 0, "errors": 0, "prompt": 0, "cached": 0, "out": 0, "tps": [], "ttft": []}
        models = collections.defaultdict(lambda: {**empty, "tps": [], "ttft": []})
        keys = collections.defaultdict(lambda: {"requests": 0, "quota": 0.0, "prompt": 0, "cached": 0, "out": 0,
                                                "limit_days": set(), "slices": [0.0] * slices})
        load, tokens, status, sessions = [0] * slices, collections.Counter(), collections.Counter(), set()
        returns = {"count": 0, "gaps": [0] * (len(GAPS) + 1), "prompt": 0, "cached": 0}
        for r in rows:
            part = min(slices - 1, int((r["time"] - start) / step))
            cost = profile.quota_cost(r) if profile and r["profile"] == profile.NAME.lower() else 0
            load[part] += 1
            status[r["status"]] += 1
            if r["session"] not in ("-", None):
                sessions.add(r["session"][-8:])
            for name in ("prompt", "cached", "out"):
                tokens[name] += r.get(name) or 0
            if r.get("back") is not None and r["status"] < 400:  # answered ones only: a parked attempt and its retry carry the same gap
                returns["count"] += 1
                returns["gaps"][bisect.bisect(GAPS, r["back"])] += 1
                returns["prompt"] += r.get("prompt") or 0
                returns["cached"] += r.get("cached") or 0
            tokens["quota"] += cost
            m = models[r["model"]]
            m["requests"] += 1
            m["errors"] += r["status"] >= 400
            m["prompt"] += r.get("prompt") or 0
            m["out"] += r.get("out") or 0
            if r.get("tps"):
                m["tps"].append(r["tps"])
            if r.get("ttft"):
                m["ttft"].append(r["ttft"])
            if r["key"] != "-":
                k = keys[r["key"]]
                k["requests"] += 1
                k["quota"] += cost
                k["prompt"] += r.get("prompt") or 0
                k["cached"] += r.get("cached") or 0
                k["out"] += r.get("out") or 0
                k["slices"][part] += cost if profile else 1
                if r["daily_limit"]:
                    k["limit_days"].add(datetime.date.fromtimestamp(r["time"]))
        peak_requests, peak_agents = peaks(rows)
        result = {"start": start, "step": step, "requests": len(rows), "agents": len(sessions),
                  "peak_requests": peak_requests, "peak_agents": peak_agents, "tokens": tokens, "status": status,
                  "models": models, "keys": keys, "load": load, "returns": returns,
                  "pace": profile.DAILY_LIMIT * step / 86400 if profile else max(
                      [max(k["slices"]) for k in keys.values()], default=0) or 1}
        with self.lock:
            if self.version == version:
                self.cached[(seconds, slices)] = (now, result)
        return result
