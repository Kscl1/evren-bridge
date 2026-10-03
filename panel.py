"""Live terminal panel for evren-bridge. Tabs (left/right): the live view, every request, statistics over a time range.
L switches between Turkish and English. Quota columns and numbers appear only when a profile gives them."""
import collections
import datetime
import os
import threading
import time

from rich import box
from rich.color import Color, blend_rgb
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from stats import percentile

# One colour per model everywhere, the profile's own or the next free one of these; green, yellow, orange and red
# stay reserved for good, fair, weak and error.
PALETTE = ("#5fafff", "#d787ff", "#5fd7d7", "#87afaf", "#af87ff", "#87afd7")  # none is a state colour
ASSIGNED = {}  # model id -> its palette colour, kept for the whole run
HEADER_MODELS = 4  # models that fit the live tab's header
MODEL_ROWS = 8  # most rows of the statistics tab's models table
GREEN, YELLOW, ORANGE, RED, TEAL = "#87d787", "#ffd75f", "#ffaf5f", "#ff5f5f", "#5fd7d7"
BEST = "bold #ffffff on #1f5f3f"  # the best value of a column
ACTIVE = "bold #0c0c0c on #61d6d6"  # the chosen tab and range
BACKGROUND = Color.parse("#0c0c0c").get_truecolor()
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"  # one cell wide in every terminal, unlike emoji
BLOCKS = "▁▂▃▄▅▆▇█"
RANGES = (600, 3600, 86400, 7 * 86400, 14 * 86400, None)  # statistics tab, None = all time
PAGE = 15  # agents per page in the live tab
STATES = {  # agent state -> (colour, animated); the order is the sort order of the agents box
    "writing": ("green", True), "calling": ("yellow", True), "thinking": ("#ff87d7", True),
    "waiting": ("dodger_blue1", True), "tool": ("yellow", False), "error": ("red", False), "idle": ("grey62", False),
}
ORDER = list(STATES)

WORDS = {
    "tr": {
        "tabs": ("İzleme", "İstekler", "İstatistikler"),
        "hints": ("←/→ sekme · Tab aktif/boşta · ↑/↓ sayfa · L dil · q çıkış", "←/→ sekme · ↑/↓ kaydır · Home canlı · L dil · q çıkış",
                  "←/→ sekme · ↑/↓ aralık · L dil · q çıkış"),
        "agents_n": "ajan", "agent_n": "ajan", "flying": "akan", "up": "açık", "pending": "durum bekleniyor", "not_listed": "listelenmiyor",
        "no_models": "henüz istek yok", "upstream": "Sağlayıcı", "tokens_min": "Token/dk", "tokens_total": "Token",
        "request_slices": "İstek · dilimlere göre",
        "accounts": "Hesaplar", "account": "Hesap", "status": "Durum", "agents_col": "Ajan", "active": "Aktif",
        "idle_col": "Boşta",
        "rpm": "İst/dk", "daily": "Günlük", "left": "kaldı", "limited": "limitte", "working": "çalışıyor",
        "connected": "bağlı", "empty": "boş",
        "agents": "Ajanlar", "agent": "Ajan", "model": "Model", "elapsed": "Süre", "ttft_short": "İlk tok",
        "context": "Bağlam", "cache": "Önb", "no_active": "çalışan ajan yok", "no_idle": "boşta ajan yok", "page": "sayfa",
        "writing": "yazıyor", "calling": "araç: {tool}", "thinking": "düşünüyor", "waiting": "yanıt bekliyor",
        "tool": "{tool} çalışıyor", "error": "hata {status}", "idle": "boşta", "units": ("s", "dk", "sa"),
        "latest": "Son istekler", "requests_title": "İstekler", "paused": "duraklatıldı (Home: canlıya dön)",
        "time": "Saat", "code": "Kod", "input": "Girdi", "output": "Çıktı", "took": "Süre", "note": "Not",
        "notes": {"daily limit": "günlük limit", "window limit": "dakika limiti", "masked stream error": "gizli 503",
                  "upstream error": "sağlayıcı yok", "client closed the connection": "istemci gitti",
                  "all keys parked": "hepsi limitte"},
        "range": "Aralık", "ranges": ("10 dk", "1 saat", "24 saat", "7 gün", "14 gün", "Tümü"),
        "summary": "Özet · son {range}", "summary_all": "Özet · tüm zamanlar · başlangıç {since}",
        "requests": "İstek", "peak_agents": "Aynı anda en çok ajan", "peak_requests": "Aynı anda en çok istek",
        "quota_used": "Kota harcaması", "input_cache": "Girdi · önbellekten",
        "returns": "Geri dönen ajan", "return_gaps": "Ara · <5 / 5–30 / 30+ dk", "return_cache": "Dönüşte önbellek",
        "models": "Modeller", "total": "Toplam", "avg_tps": "Ort. TPS", "slow": "En yavaş %10",
        "ttft": "İlk token", "errors": "Hata", "quota": "Kota", "share": "Pay", "limit_hit": "Kota doldu",
        "trend": "Kota · limite göre", "day": "gün", "days": "gün", "output_total": "Çıktı", "percent": "%{}",
        "load": "Yoğunluk · {range} · dilim {step} · en yoğun {when} ({n} istek)", "load_empty": "Yoğunluk · {range}",
        "all": "tüm zamanlar", "last": "son {range}", "durations": ("sn", "dk", "saat", "gün"),
        "no_stats": "istatistik yok",
    },
    "en": {
        "tabs": ("Live", "Requests", "Statistics"),
        "hints": ("←/→ tab · Tab active/idle · ↑/↓ page · L language · q quit", "←/→ tab · ↑/↓ scroll · Home live · L language · q quit",
                  "←/→ tab · ↑/↓ range · L language · q quit"),
        "agents_n": "agents", "agent_n": "agent", "flying": "in flight", "up": "up", "pending": "status pending", "not_listed": "not listed",
        "no_models": "no requests yet", "upstream": "Upstream", "tokens_min": "Tokens/min", "tokens_total": "Tokens",
        "request_slices": "Requests · per slice",
        "accounts": "Accounts", "account": "Account", "status": "Status", "agents_col": "Agents", "active": "Active",
        "idle_col": "Idle",
        "rpm": "Req/min", "daily": "Daily", "left": "left", "limited": "limit", "working": "working",
        "connected": "connected", "empty": "idle",
        "agents": "Agents", "agent": "Agent", "model": "Model", "elapsed": "Time", "ttft_short": "1st tok",
        "context": "Context", "cache": "Cache", "no_active": "no agent working", "no_idle": "no idle agents", "page": "page",
        "writing": "writing", "calling": "tool: {tool}", "thinking": "thinking", "waiting": "waiting",
        "tool": "running {tool}", "error": "error {status}", "idle": "idle", "units": ("s", "m", "h"),
        "latest": "Latest requests", "requests_title": "Requests", "paused": "paused (Home: back to live)",
        "time": "Time", "code": "Code", "input": "In", "output": "Out", "took": "Took", "note": "Note",
        "notes": {"daily limit": "daily limit", "window limit": "minute limit", "masked stream error": "masked 503",
                  "upstream error": "no upstream", "client closed the connection": "client left",
                  "all keys parked": "all limited"},
        "range": "Range", "ranges": ("10 min", "1 hour", "24 hours", "7 days", "14 days", "All"),
        "summary": "Summary · last {range}", "summary_all": "Summary · all time · since {since}",
        "requests": "Requests", "peak_agents": "Most agents at once", "peak_requests": "Most requests at once",
        "quota_used": "Quota used", "input_cache": "Input · from cache",
        "returns": "Agents back", "return_gaps": "Gap · <5 / 5–30 / 30+ min", "return_cache": "Cache on return",
        "models": "Models", "total": "Total", "avg_tps": "Avg TPS", "slow": "Slowest 10%",
        "ttft": "First token", "errors": "Errors", "quota": "Quota", "share": "Share", "limit_hit": "Limit hit",
        "trend": "Quota · vs limit", "day": "day", "days": "days", "output_total": "Output", "percent": "{}%",
        "load": "Load · {range} · {step} slices · busiest {when} ({n} requests)", "load_empty": "Load · {range}",
        "all": "all time", "last": "last {range}", "durations": ("s", "min", "h", "days"),
        "no_stats": "no statistics",
    },
}


def bar(used, cap, width=8):
    if not cap:
        return Text("—".ljust(width), style="grey35")
    ratio = min(1.0, (used or 0) / cap)
    color = GREEN if ratio < 0.6 else YELLOW if ratio < 0.85 else RED
    filled = round(ratio * width)
    return Text.assemble(("━" * filled, color), ("━" * (width - filled), "grey27"))


def short(n):
    if n is None:
        return "—"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k" if n < 10_000 else f"{n / 1_000:.0f}k"
    return str(round(n))


def clock(ts):
    return datetime.datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def ago(seconds, w):
    seconds, (s, m, h) = int(seconds), w["units"]
    return f"{seconds}{s}" if seconds < 60 else f"{seconds // 60}{m}" if seconds < 3600 else f"{seconds // 3600}{h}"


def took(seconds, w):
    """A request's duration in five cells: 12.3s, then 141s, then 16dk."""
    if seconds < 99.95:  # 99.96 would round to 100.0s
        return f"{seconds:.1f}s"
    return f"{seconds:.0f}{w['units'][0]}" if seconds < 999.5 else ago(seconds, w)


def span(seconds, w):
    """A slice length in words: 25 sn, 2 dk, 1 saat, 7 gün."""
    s, m, h, d = w["durations"]
    if seconds < 60:
        return f"{seconds:.0f} {s}"
    if seconds < 3600:
        return f"{seconds / 60:.0f} {m}"
    if seconds < 86400:
        return f"{seconds / 3600:.0f} {h}"
    return f"{seconds / 86400:.0f} {d}"


def model_color(model, profile):
    if not model:
        return ""
    if profile and model in profile.MODELS:
        return profile.MODELS[model]
    if model not in ASSIGNED:
        ASSIGNED[model] = PALETTE[len(ASSIGNED) % len(PALETTE)]
    return ASSIGNED[model]


def model_name(model, profile):
    short = profile.SHORT_NAMES.get(model) if profile else None
    return Text(short or model or "-", style=model_color(model, profile))


def models_in_use(events, agents, profile):
    """Model ids to show: the profile's first, then those seen in requests, most used first."""
    seen = collections.Counter(e["model"] for e in events if e.get("model"))
    seen.update(a["model"] for a in agents if a.get("model"))
    first = list(profile.MODELS) if profile else []
    return first + [m for m, _ in seen.most_common() if m not in first]


def grade(value, good, fair, text, higher_is_better=True, worst=ORANGE):
    """Green, yellow or `worst` by two thresholds."""
    if value is None:
        return Text("—")
    if higher_is_better:
        color = GREEN if value >= good else YELLOW if value >= fair else worst
    else:
        color = GREEN if value < good else YELLOW if value < fair else worst
    return Text(text, style=color)


def tps_text(tps):
    return grade(tps, 80, 30, f"{tps:.0f}") if tps else Text("—")


def ttft_text(ttft):
    return grade(ttft, 1, 3, f"{ttft:.1f}s", higher_is_better=False) if ttft else Text("—")


def percent(value, w, digits=0):
    """%94 in Turkish, 94% in English."""
    return w["percent"].format(f"{value:.{digits}f}")


def cache_text(ratio, w):
    return grade(ratio, 0.8, 0.5, percent(100 * ratio, w)) if ratio is not None else Text("—")


def error_text(errors, requests, w):
    if not requests:
        return Text("—")
    rate = 100 * errors / requests
    return grade(rate, 1, 5, percent(rate, w, 1), higher_is_better=False, worst=RED)


def days(n, w):
    return f"{n} {w['day'] if n == 1 else w['days']}"


def shade(color, level):
    """The colour faded towards the terminal background; level 0..1."""
    return blend_rgb(BACKGROUND, Color.parse(color).get_truecolor(), 0.25 + 0.75 * max(0.0, min(1.0, level))).hex


def frame(now):
    return SPINNER[int(now * 8) % len(SPINNER)]


def account_columns(label_width, profile=None):
    columns = (("account", label_width, "left"), ("status", 13, "left"), ("active", 6, "right"),
               ("idle_col", 6, "right"), ("rpm", 7, "right"))
    if not profile:  # one column as wide as the three quota ones
        return columns + (("tokens_min", 54, "left"),)
    return columns + ((f"TPM / {short(profile.TPM_LIMIT)}", 14, "left"),
                      (f"TP5M / {short(profile.WINDOW_LIMIT)}", 14, "left"), ("daily", 20, "left"))


def agent_columns(label_width):
    return (("agent", 8, "left"), ("account", label_width, "left"), ("model", 18, "left"), ("status", 24, "left"),
            ("elapsed", 5, "right"), ("TPS", 4, "right"), ("ttft_short", 7, "right"), ("context", 7, "right"),
            ("cache", 5, "right"))


def feed_columns(label_width):
    return (("time", 8, "left"), ("agent", 8, "left"), ("account", label_width, "left"), ("model", 18, "left"),
            ("code", 4, "right"), ("input", 5, "right"), ("cache", 5, "right"), ("output", 5, "right"),
            ("TPS", 4, "right"), ("took", 5, "right"), ("note", 13, "left"))


def model_stat_columns(label_width):
    return (("model", 19, "left"), ("requests", 8, "right"), ("input", 7, "right"), ("output", 7, "right"),
            ("total", 7, "right"), ("avg_tps", 8, "right"), ("slow", 12, "right"), ("ttft", 11, "right"),
            ("errors", 6, "right"))


def account_stat_columns(label_width, profile=None):
    if not profile:  # as wide as the quota columns
        return (("account", label_width, "left"), ("requests", 8, "right"), ("input", 7, "right"),
                ("output", 7, "right"), ("cache", 5, "right"), ("request_slices", 36, "left"))
    return (("account", label_width, "left"), ("requests", 8, "right"), ("quota", 7, "right"), ("share", 6, "right"),
            ("cache", 5, "right"), ("limit_hit", 10, "right"), ("trend", 24, "left"))


def width_of(columns):
    """Inner width of a table with these columns: their widths plus padding and a divider between neighbours."""
    return sum(width for _, width, _ in columns) + 3 * (len(columns) - 1)


def section(body, title, width):
    return Panel(body, title=Text(f" {title} ", style="bold cyan"), title_align="left", box=box.ROUNDED,
                 border_style="grey35", width=width + 4, padding=(0, 1))


def table(columns, w):
    """A table whose columns keep a fixed width, so rows line up whatever their content or language."""
    t = Table(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False, header_style="grey62", expand=True)
    for name, width, justify in columns:
        t.add_column(w.get(name, name), width=width, justify=justify, no_wrap=True, overflow="ellipsis")
    return t


def header(router, rows, agents, events, now, width, w, profile):
    up = int(now - router.started)
    top = Table.grid(expand=True)
    top.add_column()
    top.add_column(justify="right")
    top.add_row(
        Text.assemble(("evren-bridge", "bold cyan"), ("  ·  ", "grey50"),
                      datetime.datetime.fromtimestamp(now).strftime("%d.%m.%Y  %H:%M:%S")),
        Text.assemble((f"{len(agents)}", "bold"), f" {w['agent_n' if len(agents) == 1 else 'agents_n']}  ·  ", (f"{sum(r['in_flight'] for r in rows)}", "bold"),
                      f" {w['flying']}  ·  ", f"{w['up']} {up // 3600}:{up % 3600 // 60:02d}", style="grey62"))
    names = models_in_use(events, agents, profile)[:HEADER_MODELS]
    cells = [model_cell(router, m, agents, events, now, w, profile) for m in names]
    if not cells:
        cells = [Text.assemble(("○ ", "grey50"), (w["no_models"], "grey62"), "\n")]
    models = Table.grid(expand=True)
    for _ in range(HEADER_MODELS):
        models.add_column(ratio=1, no_wrap=True)
    models.add_row(*cells, *[Text("")] * (HEADER_MODELS - len(cells)))
    return section(Group(top, Text(""), models), profile.NAME if profile else w["upstream"], width)


def model_cell(router, model, agents, events, now, w, profile):
    listed = router.listed  # one read: the quota thread may replace it
    if listed is None:
        dot, color, info, info_style = "○", "grey50", w["pending"], "grey62"
    elif model not in listed:  # says nothing about the model's health
        dot, color, info, info_style = "○", "grey50", w["not_listed"], "grey62"
    else:
        speeds = [e["tps"] for e in events if e["model"] == model and e.get("tps") and now - e["time"] < 300]
        users = sum(1 for a in agents if a.get("model") == model)
        dot, color, info_style = "●", GREEN, "grey62"
        info = f"{users} {w['agent_n' if users == 1 else 'agents_n']}" + (f" · {sum(speeds) / len(speeds):.0f} tps" if speeds else "")
    return Text.assemble((f"{dot} ", color), (model, f"bold {model_color(model, profile)}"), "\n",
                         (f"  {info}", info_style))


def accounts(router, rows, events, label_width, width, w, profile, now):
    t = table(account_columns(label_width, profile), w)
    tokens = collections.Counter()  # per key label: input and output tokens of the last minute's requests
    for e in events:
        if now - e["time"] < 60 and e["usage"]:
            tokens[e["key"]] += (e["usage"].get("prompt") or 0) + (e["usage"].get("out") or 0)
    for r in rows:
        quota = r["quota"] or {}
        if r["parked_until"]:
            state = Text(f"{w['limited']} {clock(r['parked_until'])[:5]}", style=RED)
        elif quota.get("error"):
            state = Text(f"✗ {quota['error']}", style=RED)
        elif r["in_flight"]:
            state = Text(f"● {w['working']}", style=GREEN)
        elif r["active"] or r["idle"]:
            state = Text(f"● {w['connected']}", style="cyan")
        else:
            state = Text(f"○ {w['empty']}", style="grey50")
        daily_left = quota.get("daily_remaining_tokens")
        cells = [Text(r["label"], style="bold" if r["active"] or r["idle"] else "grey62"), state,
                 Text(f"{r['active']}/{router.active_cap}", style=YELLOW if r["active"] >= router.active_cap else ""),
                 Text(str(r["idle"]), style="grey62"),
                 str(r["requests_per_min"])]
        if not profile:
            t.add_row(*cells, short(tokens[r["label"]]))
            continue
        t.add_row(
            *cells,
            Text.assemble(bar(r["tpm"], profile.TPM_LIMIT), f" {short(r['tpm']):>5}"),
            Text.assemble(bar(quota.get("used_tokens"), quota.get("cap")), f" {short(quota.get('used_tokens')):>5}"),
            Text.assemble(bar(quota.get("daily_used_tokens"), quota.get("daily_limit_tokens")),
                          f" {short(daily_left):>5} {w['left']}" if daily_left is not None else ""),
        )
    return section(t, w["accounts"], width)


def agent_state(agent, now, w):
    state = agent.get("state")
    color, animated = STATES.get(state, ("grey50", False))
    label = w.get(state, "?").format(tool=agent.get("tool") or "…", status=agent.get("status", ""))
    mark = frame(now) if animated else {"tool": "▸", "error": "✗", "idle": "✓"}.get(state, "·")
    return Text(f"{mark} {label}", style=color)


def agents_section(listed, size, view, label_width, now, width, w, counts, profile):
    """One page of the chosen sub-tab, Active or Idle; with more than one page the box keeps its height."""
    t = table(agent_columns(label_width), w)
    pages = max(1, -(-len(listed) // size))
    view.page = min(view.page, pages - 1)
    shown = listed[view.page * size:(view.page + 1) * size]
    for a in shown:
        t.add_row(a["session"][-8:], a.get("key", "-"), model_name(a.get("model"), profile), agent_state(a, now, w),
                  ago(now - a["since"], w) if a.get("since") else "—", tps_text(a.get("tps")),
                  ttft_text(a.get("ttft")), short(a.get("context")), cache_text(a.get("cache"), w))
    if not listed:  # notes go in the wide status column so they are never cut
        t.add_row("", "", "", Text(w["no_idle" if view.idle_tab else "no_active"], style="grey50"), *[""] * 5)
    if pages > 1:
        for _ in range(size - len(shown)):
            t.add_row(*[""] * 9)
    title = Text.assemble((f" {w['agents']} ", "bold cyan"), " ",
                          (f" {w['active']} {counts[0]} ", "grey62" if view.idle_tab else ACTIVE), " ",
                          (f" {w['idle_col']} {counts[1]} ", ACTIVE if view.idle_tab else "grey62"))
    subtitle = Text(f" {w['page']} {view.page + 1}/{pages}" + ("  ↑↓ " if pages > 1 else " "), style="grey62")
    return Panel(t, title=title, title_align="left", subtitle=subtitle, subtitle_align="right", box=box.ROUNDED,
                 border_style="grey35", width=width + 4, padding=(0, 1))


def feed_section(events, label_width, width, title, w, profile):
    """Requests, newest first."""
    t = table(feed_columns(label_width), w)
    for e in events:
        usage = e["usage"] or {}
        status = e["status"]
        t.add_row(clock(e["time"]), e["session"][-8:], e["key"], model_name(e["model"], profile),
                  Text(str(status), style=GREEN if status < 300 else YELLOW if status == 429 else RED),
                  short(usage.get("prompt")), short(usage.get("cached")), short(usage.get("out")),
                  tps_text(e.get("tps")), took(e["seconds"], w),
                  Text(w["notes"].get(e["note"].split(",")[0], e["note"]), style="grey50"))
    return section(t, title, width)


def monitor_tab(router, height, view, now, label_width, width, w, profile):
    rows = router.status()
    agents = router.visible_agents()
    events = list(router.events)
    active = sorted((a for a in agents if a["active"]), key=lambda a: (
        ORDER.index(a["state"]) if a.get("state") in ORDER else len(ORDER), -(a.get("since") or 0)))
    idle = sorted((a for a in agents if not a["active"]), key=lambda a: -(a.get("since") or 0))  # newest first
    listed = idle if view.idle_tab else active
    left = height - 6 - (len(rows) + 4)  # rows left after the header box and the accounts box
    size = max(1, min(PAGE, left - 8 - 4))  # keep at least 4 request rows below
    agent_rows = size if len(listed) > size else max(1, len(listed))
    latest = events[::-1][:max(0, left - 8 - agent_rows)]
    return Group(header(router, rows, active, events, now, width, w, profile),
                 accounts(router, rows, events, label_width, width, w, profile, now),
                 agents_section(listed, size, view, label_width, now, width, w, (len(active), len(idle)), profile),
                 feed_section(latest, label_width, width, w["latest"], w, profile))


def requests_tab(router, height, view, label_width, width, w, profile):
    rows = max(1, height - 4)
    with router.lock:
        events, count = list(router.events)[::-1], router.event_count
    if view.request_offset == 0:
        view.request_anchor = count
    start = min(view.request_offset + count - view.request_anchor, max(0, len(events) - rows))
    view.request_offset = max(0, start - (count - view.request_anchor))
    shown = events[start:start + rows]
    title = f"{w['requests_title']}  {start + 1 if shown else 0}–{start + len(shown)} / {len(events)}"
    if view.request_offset:
        title += f"  ·  {w['paused']}"
    return feed_section(shown, label_width, width, title, w, profile)


def best(text, is_best):
    return Text(text.plain, style=BEST) if is_best else text


def strip(values, color, top):
    """One cell per slice, brighter for larger values; a dot where there is nothing."""
    text = Text()
    for v in values:
        text.append("█" if v else "·", style=shade(color, v / top) if v else "grey35")
    return text


def stats_tab(stats, router, view, label_width, width, w, now, profile):
    seconds = RANGES[view.range]
    s = stats.summary(seconds, now)
    range_name = w["ranges"][view.range]
    when = w["last"].format(range=range_name) if seconds else w["all"]

    chooser = Text(f" {w['range']}  ", style="grey62")
    for n, name in enumerate(w["ranges"]):
        chooser.append(f" {name} ", style=ACTIVE if n == view.range else "grey62")
        chooser.append(" ")

    cards = Table.grid(expand=True)
    for _ in range(4):
        cards.add_column(ratio=1)

    def card(label, value, style="bold"):
        return Text.assemble((f"{label}\n", "grey62"), (value, style))

    tokens, status = s["tokens"], s["status"]
    cache = tokens["cached"] / tokens["prompt"] if tokens["prompt"] else None
    server_errors = sum(n for code, n in status.items() if code >= 500)
    cards.add_row(card(w["requests"], f"{s['requests']:,}"), card(w["agents_col"], f"{s['agents']:,}"),
                  card(w["peak_agents"], str(s["peak_agents"])), card(w["peak_requests"], str(s["peak_requests"])))
    cards.add_row(Text(""), Text(""), Text(""), Text(""))
    spent = card(w["quota_used"], short(tokens["quota"])) if profile else card(w["tokens_total"],
                                                                                short(tokens["prompt"] + tokens["out"]))
    cards.add_row(spent,
                  Text.assemble((f"{w['input_cache']}\n", "grey62"), (f"{short(tokens['prompt'])} · ", "bold"),
                                cache_text(cache, w)),
                  card(w["output_total"], short(tokens["out"])),
                  card("429 · 5xx", f"{status[429]} · {server_errors}",
                       f"bold {RED if server_errors else YELLOW if status[429] else GREEN}"))
    back = s["returns"]
    cards.add_row(Text(""), Text(""), Text(""), Text(""))
    cards.add_row(card(w["returns"], f"{back['count']:,}"), card(w["return_gaps"], " / ".join(map(str, back["gaps"]))),
                  Text.assemble((f"{w['return_cache']}\n", "grey62"),
                                cache_text(back["cached"] / back["prompt"] if back["prompt"] else None, w)), Text(""))
    since = datetime.datetime.fromtimestamp(s["start"]).strftime("%d.%m.%Y")
    summary_title = w["summary"].format(range=range_name) if seconds else w["summary_all"].format(since=since)

    models = table(model_stat_columns(label_width), w)
    names = list(profile.MODELS) if profile else []
    names += sorted((m for m in s["models"] if m not in names and m not in ("", "-")),
                    key=lambda m: -s["models"][m]["requests"])
    rows = []
    for name in names[:MODEL_ROWS]:
        m = s["models"].get(name)
        if not m:
            rows.append((name, None))
            continue
        rows.append((name, {"requests": m["requests"], "input": m["prompt"], "output": m["out"],
                            "total": m["prompt"] + m["out"], "avg": sum(m["tps"]) / len(m["tps"]) if m["tps"] else None,
                            "slow": percentile(m["tps"], 0.1), "ttft": sum(m["ttft"]) / len(m["ttft"]) if m["ttft"] else None,
                            "errors": m["errors"]}))
    present = [v for _, v in rows if v]

    def top_of(field, highest=True):
        values = [v[field] for v in present if v[field] is not None]
        return (max if highest else min)(values) if values else None

    tops = {f: top_of(f) for f in ("requests", "total", "avg", "slow")}
    tops["ttft"] = top_of("ttft", highest=False)
    tops["error_rate"] = min((v["errors"] / v["requests"] for v in present), default=None)
    for name, v in rows:
        if not v:
            models.add_row(Text(name, style=model_color(name, profile)), *[Text("—")] * 8)
            continue
        models.add_row(
            Text(name, style=model_color(name, profile)),
            best(Text(f"{v['requests']:,}"), v["requests"] == tops["requests"]),
            short(v["input"]), short(v["output"]),
            best(Text(short(v["total"])), v["total"] == tops["total"]),
            best(tps_text(v["avg"]), v["avg"] is not None and v["avg"] == tops["avg"]),
            best(tps_text(v["slow"]), v["slow"] is not None and v["slow"] == tops["slow"]),
            best(ttft_text(v["ttft"]), v["ttft"] is not None and v["ttft"] == tops["ttft"]),
            best(error_text(v["errors"], v["requests"], w), v["errors"] / v["requests"] == tops["error_rate"]))

    accounts_table = table(account_stat_columns(label_width, profile), w)
    total_quota = tokens["quota"] or 1
    for label, _ in router.keys:
        k = s["keys"].get(label)
        if not profile:
            k = k or {"requests": 0, "prompt": 0, "cached": 0, "out": 0, "slices": [0] * len(s["load"])}
            accounts_table.add_row(
                Text(label, style="bold" if k["requests"] else "grey62"), f"{k['requests']:,}", short(k["prompt"]),
                short(k["out"]), cache_text(k["cached"] / k["prompt"] if k["prompt"] else None, w),
                strip(k["slices"], TEAL, s["pace"]))
            continue
        if not k:
            accounts_table.add_row(Text(label, style="grey62"), "0", "0", "—", "—", Text(days(0, w), style="grey62"),
                                   strip([0] * len(s["load"]), TEAL, 1))
            continue
        accounts_table.add_row(
            Text(label, style="bold"), f"{k['requests']:,}", short(k["quota"]), percent(100 * k["quota"] / total_quota, w),
            cache_text(k["cached"] / k["prompt"] if k["prompt"] else None, w),
            Text(days(len(k["limit_days"]), w), style=RED if k["limit_days"] else "grey62"),
            strip(k["slices"], TEAL, s["pace"]))

    load, step = s["load"], s["step"]
    peak = max(load)
    chart = Text()
    for level in range(3, -1, -1):  # four rows of eighths, top row first
        for value in load:
            fill = max(0, min(8, round(value / (peak or 1) * 32) - level * 8))
            chart.append(" " + (BLOCKS[fill - 1] * 3 if fill else "   "), style=shade(TEAL, 0.35 + 0.65 * value / (peak or 1)))
        chart.append("\n")
    axis = [" "] * (4 * len(load))
    fmt = "%H:%M" if step * len(load) <= 86400 else "%d.%m"
    for n in (0, 6, 12, 18):
        label = datetime.datetime.fromtimestamp(s["start"] + n * step).strftime(fmt)
        axis[4 * n + 1:4 * n + 1 + len(label)] = label
    chart.append("".join(axis), style="grey50")
    if peak:
        busiest = datetime.datetime.fromtimestamp(s["start"] + load.index(peak) * step)
        load_title = w["load"].format(range=when, step=span(step, w), n=peak,
                                      when=busiest.strftime("%H:%M" if step * len(load) <= 86400 else "%d.%m %H:%M"))
    else:
        load_title = w["load_empty"].format(range=when)

    chooser_line = Table(box=None, show_header=False, width=width + 4, padding=0)  # as wide as the boxes below
    chooser_line.add_column()
    chooser_line.add_row(chooser)
    chooser_line.add_row("")
    models_title = w["models"] + (f"  {MODEL_ROWS} / {len(names)}" if len(names) > MODEL_ROWS else "")
    return Group(chooser_line, section(cards, summary_title, width), section(models, models_title, width),
                 section(accounts_table, w["accounts"], width), section(chart, load_title, width))


class View:
    """What the viewer chose with the keyboard: tab, language, statistics range, agents page and requests scroll."""

    def __init__(self, lang="tr"):
        self.tab = 0
        self.lang = lang
        self.range = 2  # 24 hours
        self.idle_tab = False  # the live tab's agents box: Active or Idle
        self.page = 0
        self.request_offset = 0  # rows scrolled down from the newest request; 0 follows the live stream
        self.request_anchor = 0  # router.event_count when scrolling started, so the view stays still as requests arrive
        self.quit = False

    def key(self, name):
        if name == "q":
            self.quit = True
        elif name == "l":
            self.lang = "en" if self.lang == "tr" else "tr"
        elif name in ("left", "right"):
            self.tab = (self.tab + (1 if name == "right" else -1)) % 3
        elif name in ("1", "2", "3"):
            self.tab = int(name) - 1
        elif name == "\t" and self.tab == 0:
            self.idle_tab, self.page = not self.idle_tab, 0
        elif name in ("up", "down", "pgup", "pgdn", "home"):
            step = {"up": -1, "down": 1, "pgup": -10, "pgdn": 10}.get(name, 0)
            if self.tab == 0:  # a page at a time; the box clamps the last page
                self.page = 0 if name == "home" else max(0, self.page + (1 if step > 0 else -1))
            elif self.tab == 1:
                self.request_offset = 0 if name == "home" else max(0, self.request_offset + step)
            else:
                self.range = 0 if name == "home" else max(0, min(len(RANGES) - 1, self.range + step))


def read_keys(view):
    """Feed arrow keys and letters to the view until it quits (Windows console, or a POSIX terminal)."""
    if os.name == "nt":
        import msvcrt
        arrows = {"H": "up", "P": "down", "K": "left", "M": "right", "I": "pgup", "Q": "pgdn", "G": "home"}
        while not view.quit:
            ch = msvcrt.getwch()
            view.key(arrows.get(msvcrt.getwch(), "") if ch in ("\x00", "\xe0") else "q" if ch == "\x03" else ch.lower())
        return
    import sys
    arrows = {"[A": "up", "[B": "down", "[D": "left", "[C": "right", "[5": "pgup", "[6": "pgdn", "[H": "home"}
    while not view.quit:  # run() has put the terminal in cbreak mode
        ch = sys.stdin.read(1)
        if ch == "\x1b":
            code = sys.stdin.read(2)
            if code[-1] in "56":
                sys.stdin.read(1)  # the trailing ~ of page up/down
            view.key(arrows.get(code, ""))
        else:
            view.key(ch.lower())


def tab_bar(view, width, w):
    tabs = Text()
    for n, name in enumerate(w["tabs"]):
        tabs.append(f" {name} ", style=ACTIVE if n == view.tab else "grey62")
        tabs.append(" ")
    line = Table.grid(expand=True)
    line.add_column()
    line.add_column(justify="right")
    line.add_row(tabs, Text(w["hints"][view.tab] + " ", style="grey50"))
    return Panel(line, box=box.SIMPLE, width=width + 4, padding=(0, 1))


def render(router, height, now=None, view=None, stats=None, profile=None):
    now = now or time.time()
    view = view or View()
    w = WORDS[view.lang]
    label_width = max(len(WORDS["tr"]["account"]), len(WORDS["en"]["account"]),
                      *(len(label) for label, _ in router.keys))
    width = max(width_of(columns(label_width)) for columns in (account_columns, agent_columns, feed_columns,
                                                                model_stat_columns, account_stat_columns))
    height -= 3  # the tab bar
    if view.tab == 1:
        body = requests_tab(router, height, view, label_width, width, w, profile)
    elif view.tab == 2:
        body = stats_tab(stats, router, view, label_width, width, w, now, profile) if stats \
            else Text(w["no_stats"], style="grey50")
    else:
        body = monitor_tab(router, height, view, now, label_width, width, w, profile)
    return Group(tab_bar(view, width, w), body)


def run(server, stats, lang="en"):
    console = Console()
    view = View(lang)
    restore = cbreak()
    try:
        threading.Thread(target=read_keys, args=(view,), daemon=True).start()
        with Live(console=console, screen=True, refresh_per_second=8,
                  get_renderable=lambda: render(server.router, console.size.height, view=view, stats=stats,
                                                profile=server.profile)):
            while not view.quit:
                time.sleep(0.1)
    finally:
        restore()


def cbreak():
    """On a POSIX terminal, read keys one by one; return what puts the terminal back. Windows needs nothing."""
    if os.name == "nt":
        return lambda: None
    import sys
    import termios
    import tty
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    tty.setcbreak(fd)
    return lambda: termios.tcsetattr(fd, termios.TCSADRAIN, saved)
