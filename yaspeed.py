#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
YandexSpeedTestCLI — измеритель скорости интернета в терминале.
Работает через публичный API Яндекс.Интернетометра (yandex.ru/internet).

  https://github.com/begugla0/yandexspeedtestcli

Зависимостей нет — только стандартная библиотека Python 3.8+.
Проект неофициальный и никак не связан с Яндексом.

Лицензия MIT.
"""

import argparse
import http.client
import json
import os
import random
import re
import shutil
import signal
import ssl
import string
import sys
import threading
import time
from urllib import request, parse

__version__ = "1.0.0"
REPO = "https://github.com/begugla0/yandexspeedtestcli"

PROBES_URL = "https://yandex.ru/internet/api/v0/get-probes?flag_ws-conn-timeout=2000"
IP_URL = "https://ipv4-internet.yandex.net/api/v0/ip"
REGION_URL = "https://yandex.ru/internet/region"

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
BASE_HEADERS = {
    "User-Agent": UA,
    "Accept": "*/*",
    "Accept-Language": "ru,en;q=0.9",
    "Referer": "https://yandex.ru/internet",
    "Origin": "https://yandex.ru",
}
CTX = ssl.create_default_context()
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


# ============================================================== оформление

class UI:
    """Цвета, юникод-глифы и живой перерисовываемый блок."""

    SPIN_U = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    SPIN_A = "|/-\\"
    SPARK_U = " ▁▂▃▄▅▆▇█"
    SPARK_A = " .:-=+*#@"

    def __init__(self, color=None, unicode_=None, quiet=False):
        tty = sys.stdout.isatty()
        if color is None:
            color = tty and not os.environ.get("NO_COLOR")
        if unicode_ is None:
            enc = (sys.stdout.encoding or "").lower()
            unicode_ = "utf" in enc
        self.color, self.uni, self.quiet = color, unicode_, quiet
        self.live_tty = tty and not quiet
        self._lines = 0
        self.width = min(shutil.get_terminal_size((80, 24)).columns, 78)

    # ---- цвет
    def c(self, s, *codes):
        if not self.color or not codes:
            return s
        return "\x1b[" + ";".join(str(x) for x in codes) + "m" + s + "\x1b[0m"

    def dim(self, s):    return self.c(s, 2)
    def bold(self, s):   return self.c(s, 1)
    def cyan(self, s):   return self.c(s, 96)
    def blue(self, s):   return self.c(s, 94)
    def green(self, s):  return self.c(s, 92)
    def yellow(self, s): return self.c(s, 93)
    def red(self, s):    return self.c(s, 91)
    def mag(self, s):    return self.c(s, 95)

    # ---- глифы
    def g(self, uni, ascii_):
        return uni if self.uni else ascii_

    def spinner(self, i):
        f = self.SPIN_U if self.uni else self.SPIN_A
        return f[i % len(f)]

    def bar(self, frac, width, color=None):
        frac = max(0.0, min(1.0, frac))
        full = self.g("█", "#")
        empty = self.g("░", ".")
        n = int(round(frac * width))
        s = full * n
        return (color(s) if color else s) + self.dim(empty * (width - n))

    def spark(self, vals, width, color=None):
        chars = self.SPARK_U if self.uni else self.SPARK_A
        vals = vals[-width:]
        if not vals:
            return ""
        top = max(vals) or 1.0
        s = "".join(chars[min(len(chars) - 1,
                              int(v / top * (len(chars) - 1)))] for v in vals)
        s = s.rjust(width)
        return color(s) if color else s

    # ---- живой блок
    @staticmethod
    def _plain(s):
        return ANSI_RE.sub("", s)

    def live(self, lines):
        if not self.live_tty:
            return
        buf = []
        if self._lines:
            buf.append(f"\x1b[{self._lines}A")
        for ln in lines:
            if len(self._plain(ln)) > self.width + 40:
                ln = ln[: self.width + 40]
            buf.append("\x1b[2K" + ln + "\n")
        sys.stdout.write("".join(buf))
        sys.stdout.flush()
        self._lines = len(lines)

    def live_end(self, lines=None):
        if lines is not None:
            self.live(lines)
        self._lines = 0

    def say(self, s=""):
        if not self.quiet:
            print(s)

    def hide_cursor(self):
        if self.live_tty:
            sys.stdout.write("\x1b[?25l")
            sys.stdout.flush()

    def show_cursor(self):
        if self.live_tty:
            sys.stdout.write("\x1b[?25h")
            sys.stdout.flush()


def human_bits(bps):
    for unit in ("bit/s", "Kbit/s", "Mbit/s", "Gbit/s"):
        if bps < 1000:
            return f"{bps:.1f} {unit}"
        bps /= 1000
    return f"{bps:.1f} Tbit/s"


def human_bytes(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def speed_color(ui, bps):
    m = bps / 1e6
    if m >= 200:
        return ui.green
    if m >= 50:
        return ui.cyan
    if m >= 10:
        return ui.yellow
    return ui.red


BLOAT_GRADES = [(5, "A+"), (30, "A"), (60, "B"), (200, "C"), (400, "D")]


def bloat_grade(ms):
    for lim, g in BLOAT_GRADES:
        if ms < lim:
            return g
    return "F"


def grade_color(ui, g):
    return {"A+": ui.green, "A": ui.green, "B": ui.cyan,
            "C": ui.yellow, "D": ui.yellow}.get(g, ui.red)


# ================================================================= сеть

def rid(n=16):
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


def bust(url):
    return f"{url}{'&' if '?' in url else '?'}rid={rid()}"


def bust_path(path):
    return bust(path)


def split_url(url):
    p = parse.urlsplit(url)
    return p.hostname, p.port or 443, p.path + ("?" + p.query if p.query else "")


def get_json(url, timeout=20):
    req = request.Request(url, headers=BASE_HEADERS)
    with request.urlopen(req, timeout=timeout, context=CTX) as r:
        return json.loads(r.read().decode("utf-8"))


def pct(vals, p):
    s = sorted(vals)
    return s[min(len(s) - 1, int(len(s) * p))] if s else None


class Counter:
    __slots__ = ("n", "_lock")

    def __init__(self):
        self.n = 0
        self._lock = threading.Lock()

    def add(self, k):
        with self._lock:
            self.n += k


def run_window(counter, duration, on_tick=None, warmup_frac=0.25, tick=0.08):
    """Крутит окно замера, попутно отдавая наружу мгновенную скорость."""
    t0 = time.monotonic()
    series, inst = [], []
    while True:
        now = time.monotonic() - t0
        series.append((now, counter.n))
        j = len(series) - 1
        while j > 0 and series[-1][0] - series[j][0] < 0.5:
            j -= 1
        dt = series[-1][0] - series[j][0]
        cur = ((series[-1][1] - series[j][1]) * 8 / dt) if dt > 0 else 0.0
        inst.append(cur)
        if on_tick:
            on_tick(now, duration, cur, counter.n, inst)
        if now >= duration:
            break
        time.sleep(tick)

    warm = duration * warmup_frac
    lo = next((i for i, (t, _) in enumerate(series) if t >= warm), 0)
    dt = series[-1][0] - series[lo][0]
    avg = ((series[-1][1] - series[lo][1]) * 8 / dt) if dt > 0 else 0.0
    peak = 0.0
    for i, (t, b) in enumerate(series):
        for j in range(i + 1, len(series)):
            if series[j][0] - t >= 1.0:
                peak = max(peak, (series[j][1] - b) * 8 / (series[j][0] - t))
                break
    return avg, peak, series


# --------------------------------------------------------------- задержка

def _conn(host, port, timeout):
    return http.client.HTTPSConnection(host, port, timeout=timeout, context=CTX)


def ping_series(url, count, timeout=5, gap=0.03, stop=None, out=None):
    """
    Пинг по одному переиспользуемому соединению: TCP+TLS поднимаются
    прогревочным запросом до замера, поэтому в RTT остаётся только round-trip.
    Если задан stop — работает до его установки (замер под нагрузкой).
    """
    host, port, path = split_url(url)
    rtts = out if out is not None else []
    fails, c = 0, None
    try:
        c = _conn(host, port, timeout)
        c.request("GET", bust_path(path), headers=BASE_HEADERS)
        c.getresponse().read()
    except Exception:
        return None

    i = 0
    while True:
        if stop is not None and stop.is_set():
            break
        if stop is None and i >= count:
            break
        i += 1
        try:
            t = time.monotonic()
            c.request("GET", bust_path(path), headers=BASE_HEADERS)
            c.getresponse().read()
            rtts.append((time.monotonic() - t) * 1000)
        except Exception:
            fails += 1
            try:
                c.close()
            except Exception:
                pass
            try:
                c = _conn(host, port, timeout)
            except Exception:
                break
        time.sleep(gap)
    try:
        c.close()
    except Exception:
        pass

    if not rtts:
        return None
    j = (sum(abs(b - a) for a, b in zip(rtts, rtts[1:])) / (len(rtts) - 1)
         if len(rtts) > 1 else 0.0)
    return {"min": min(rtts), "avg": sum(rtts) / len(rtts),
            "median": pct(rtts, 0.5), "p95": pct(rtts, 0.95),
            "jitter": j, "n": len(rtts), "fails": fails}


class LoadedLatency(threading.Thread):
    """Пингует узел во время передачи данных — ловим bufferbloat."""

    def __init__(self, url):
        super().__init__(daemon=True)
        self.url, self.stop, self.result = url, threading.Event(), None

    def run(self):
        self.result = ping_series(self.url, 0, gap=0.05, stop=self.stop)


# --------------------------------------------------------------- загрузка

def _dl_worker(url, stop, total, node):
    """Один поток на узел: 50 МБ, затем сразу следующий запрос — как в браузере."""
    while not stop.is_set():
        try:
            req = request.Request(bust(url), headers=BASE_HEADERS)
            with request.urlopen(req, timeout=20, context=CTX) as r:
                while not stop.is_set():
                    chunk = r.read(1 << 16)
                    if not chunk:
                        break
                    total.add(len(chunk))
                    node.add(len(chunk))
        except Exception:
            if stop.is_set():
                return
            time.sleep(0.2)


def measure_download(urls_by_lid, duration, streams_per_node,
                     latency_url=None, on_tick=None):
    total, stop = Counter(), threading.Event()
    nodes = {lid: Counter() for lid in urls_by_lid}

    probe = LoadedLatency(latency_url) if latency_url else None
    if probe:
        probe.start()

    workers = [threading.Thread(target=_dl_worker,
                                args=(url, stop, total, nodes[lid]), daemon=True)
               for lid, url in urls_by_lid.items()
               for _ in range(streams_per_node)]
    for w in workers:
        w.start()
    try:
        avg, peak, _ = run_window(total, duration, on_tick)
    finally:
        stop.set()
        if probe:
            probe.stop.set()
            probe.join(timeout=2)
        for w in workers:
            w.join(timeout=1)

    return {"avg_bps": avg, "peak_bps": peak, "bytes": total.n,
            "per_node": {l: c.n for l, c in nodes.items()},
            "loaded_latency": probe.result if probe else None}


# ---------------------------------------------------------------- отдача

def _ul_chunked(post_url, stop, counter, chunk):
    host, port, path = split_url(post_url)
    buf = os.urandom(chunk)
    c = None
    try:
        c = _conn(host, port, 20)
        c.putrequest("POST", path, skip_accept_encoding=True)
        for k, v in BASE_HEADERS.items():
            c.putheader(k, v)
        c.putheader("Content-Type", "application/octet-stream")
        c.putheader("Transfer-Encoding", "chunked")
        c.endheaders()
        while not stop.is_set():
            c.send(b"%x\r\n" % len(buf) + buf + b"\r\n")
            counter.add(len(buf))
        c.send(b"0\r\n\r\n")
        c.getresponse().read()
    except Exception:
        pass
    finally:
        if c:
            try:
                c.close()
            except Exception:
                pass


def _ul_fixed(post_url, stop, counter, chunk):
    buf = os.urandom(chunk)
    while not stop.is_set():
        try:
            req = request.Request(bust(post_url), data=buf, method="POST",
                                  headers={**BASE_HEADERS,
                                           "Content-Type": "application/octet-stream"})
            with request.urlopen(req, timeout=20, context=CTX) as r:
                r.read()
            counter.add(len(buf))
        except Exception:
            if stop.is_set():
                return
            time.sleep(0.2)


def _stats_reader(stats_url, stop, frames):
    """SSE-поток сервера: data: {"k":"u","b":<байт за интервал>,"i":<мс>,"t":<ms>}"""
    try:
        req = request.Request(stats_url, headers={**BASE_HEADERS,
                                                  "Accept": "text/event-stream"})
        with request.urlopen(req, timeout=60, context=CTX) as r:
            for raw in r:
                if stop.is_set():
                    return
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                try:
                    ev = json.loads(line[5:].strip())
                except Exception:
                    continue
                ev["_mono"] = time.monotonic()
                frames.append(ev)
    except Exception:
        pass


def _score_frames(frames, t_from, t_to):
    win = [f for f in frames if t_from <= f.get("_mono", 0) <= t_to
           and isinstance(f.get("b"), (int, float))]
    if not win:
        return None
    tb = sum(f["b"] for f in win)
    tms = sum(f.get("i") or 0 for f in win)
    if tms <= 0:
        return None
    peak, ab, am, head = 0.0, 0, 0, 0
    for i, f in enumerate(win):
        ab += f["b"]
        am += f.get("i") or 0
        while am > 1000 and head < i:
            ab -= win[head]["b"]
            am -= win[head].get("i") or 0
            head += 1
        if am >= 500:
            peak = max(peak, ab * 8 / (am / 1000))
    return {"avg_bps": tb * 8 / (tms / 1000), "peak_bps": peak,
            "bytes": tb, "frames": len(win)}


def measure_upload(post_url, stats_url, duration, streams, chunk, mode,
                   latency_url=None, on_tick=None):
    worker = _ul_chunked if mode == "chunked" else _ul_fixed
    counter, stop, frames = Counter(), threading.Event(), []

    if stats_url:
        threading.Thread(target=_stats_reader,
                         args=(stats_url, stop, frames), daemon=True).start()
        time.sleep(0.4)

    probe = LoadedLatency(latency_url) if latency_url else None
    if probe:
        probe.start()

    workers = [threading.Thread(target=worker,
                                args=(post_url, stop, counter, chunk), daemon=True)
               for _ in range(streams)]
    t0 = time.monotonic()
    for w in workers:
        w.start()
    try:
        avg, peak, _ = run_window(counter, duration, on_tick)
    finally:
        t1 = time.monotonic()
        stop.set()
        if probe:
            probe.stop.set()
            probe.join(timeout=2)
        for w in workers:
            w.join(timeout=2)

    res = {"client": {"avg_bps": avg, "peak_bps": peak, "bytes": counter.n},
           "loaded_latency": probe.result if probe else None}
    srv = _score_frames(frames, t0 + duration * 0.25, t1)
    if srv:
        res["server"] = srv
        res.update(avg_bps=srv["avg_bps"], peak_bps=srv["peak_bps"],
                   bytes=srv["bytes"], source="server")
    else:
        res.update(res["client"])
        res["source"] = "client"
    return res


# ================================================================ пробники

def hosts_by_lid(latency_probes):
    m = {}
    for p in latency_probes:
        u = parse.urlsplit(p["url"])
        lid = parse.parse_qs(u.query).get("lid", [None])[0]
        if lid:
            m[lid] = u.hostname
    return m


def probes_by_lid(probe_list, predicate=lambda p: True, host_map=None):
    out = {}
    for p in probe_list:
        if not predicate(p):
            continue
        u = parse.urlsplit(p["url"])
        lid = parse.parse_qs(u.query).get("lid", [None])[0]
        if lid is None and host_map:
            lid = host_map.get(u.hostname)
        if lid and lid not in out:
            out[lid] = p
    return out


# ================================================================== вывод

def banner(ui):
    if ui.quiet:
        return
    w = ui.width
    tl, tr, bl, br, h, v = (ui.g("╭", "+"), ui.g("╮", "+"), ui.g("╰", "+"),
                            ui.g("╯", "+"), ui.g("─", "-"), ui.g("│", "|"))
    title = "yaspeed"
    sub = f"замер скорости через Яндекс.Интернетометр  v{__version__}"
    print(ui.dim(tl + h * (w - 2) + tr))
    print(ui.dim(v) + " " + ui.bold(ui.cyan(title)) + " " +
          ui.dim(sub.ljust(w - 4 - len(title))) + ui.dim(v))
    print(ui.dim(bl + h * (w - 2) + br))


def phase_tick(ui, label, color, arrow):
    """Возвращает колбэк для run_window, рисующий живой блок."""
    state = {"i": 0}

    def cb(now, dur, cur, total_bytes, inst):
        state["i"] += 1
        i = state["i"]
        prog = min(1.0, now / dur) if dur else 0
        sp = ui.spinner(i)
        head = (f"  {color(sp)}  {ui.bold(label)}  "
                f"{ui.bold(color(human_bits(cur).rjust(12)))}")
        line2 = ("     " + ui.spark(inst, min(34, ui.width - 30), color) +
                 "  " + ui.dim(human_bytes(total_bytes).rjust(8)))
        line3 = ("     " + ui.bar(prog, min(34, ui.width - 30), color) +
                 "  " + ui.dim(f"{now:4.1f}/{dur:.0f} c"))
        ui.live([head, line2, line3])

    return cb


def phase_done(ui, label, color, arrow, res, extra=""):
    ui.live_end([
        f"  {color(arrow)}  {ui.bold(label)}  "
        f"{ui.bold(color(human_bits(res['avg_bps']).rjust(12)))}"
        f"   {ui.dim('пик ' + human_bits(res['peak_bps']))}"
        f"   {ui.dim(human_bytes(res['bytes']))}{extra}"
    ])


def summary(ui, out):
    if ui.quiet:
        return
    w = ui.width
    h, v = ui.g("─", "-"), ui.g("│", "|")
    print()
    print(ui.dim(ui.g("╭", "+") + h * (w - 2) + ui.g("╮", "+")))

    def row(k, val):
        pad = w - 4 - len(ui._plain(val)) - len(k)
        print(ui.dim(v) + " " + ui.dim(k) + " " * max(1, pad) + val + " " + ui.dim(v))

    dl = out.get("download", {}).get("avg_bps", 0)
    ul = out.get("upload", {}).get("avg_bps", 0)
    top = max(dl, ul, 1)
    bw = min(26, w - 34)
    if dl:
        row("Загрузка ", ui.bar(dl / top, bw, speed_color(ui, dl)) + "  " +
            ui.bold(speed_color(ui, dl)(human_bits(dl).rjust(12))))
    if ul:
        row("Отдача   ", ui.bar(ul / top, bw, speed_color(ui, ul)) + "  " +
            ui.bold(speed_color(ui, ul)(human_bits(ul).rjust(12))))

    lat = out.get("best_latency")
    if lat:
        row("Задержка ", ui.bold(f"{lat['min']:.1f} ms") +
            ui.dim(f"  джиттер {lat['jitter']:.1f} ms"))
    bb = out.get("bufferbloat")
    if bb is not None:
        g = bloat_grade(bb)
        row("Bufferbloat ", grade_color(ui, g)(ui.bold(g)) +
            ui.dim(f"  +{bb:.0f} ms под нагрузкой"))

    print(ui.dim(ui.g("╰", "+") + h * (w - 2) + ui.g("╯", "+")))
    print(ui.dim(f"  {REPO}"))


# ==================================================================== main

def build_parser():
    p = argparse.ArgumentParser(
        prog="yaspeed",
        description="Замер скорости интернета через API Яндекс.Интернетометра.",
        epilog=f"Исходники и багрепорты: {REPO}",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("-d", "--duration", type=float, default=6.0,
                   metavar="СЕК", help="длительность каждой фазы")
    p.add_argument("--dl-nodes", choices=["all", "best"], default="all",
                   help="all — качать со всех узлов сразу, как веб-клиент")
    p.add_argument("--dl-streams", type=int, default=1, metavar="N",
                   help="потоков загрузки на каждый узел")
    p.add_argument("--ul-streams", type=int, default=4, metavar="N",
                   help="потоков отдачи")
    p.add_argument("--ul-chunk", type=int, default=256 * 1024, metavar="БАЙТ",
                   help="размер блока при отдаче")
    p.add_argument("--ul-mode", choices=["chunked", "fixed"], default="chunked",
                   help="chunked — один длинный POST, fixed — много коротких")
    p.add_argument("--pings", type=int, default=10, metavar="N",
                   help="число пингов на узел")
    p.add_argument("--node", default="best", metavar="LID",
                   help="конкретный узел вместо автовыбора")
    p.add_argument("--no-download", action="store_true", help="пропустить загрузку")
    p.add_argument("--no-upload", action="store_true", help="пропустить отдачу")
    p.add_argument("--no-bufferbloat", action="store_true",
                   help="не мерить задержку под нагрузкой")
    p.add_argument("--probes-only", action="store_true",
                   help="показать сырой ответ get-probes и выйти")
    p.add_argument("-j", "--json", action="store_true", help="вывод в JSON")
    p.add_argument("-q", "--quiet", action="store_true", help="только итог")
    p.add_argument("--no-color", action="store_true", help="без цвета")
    p.add_argument("--ascii", action="store_true", help="без юникод-графики")
    p.add_argument("-V", "--version", action="version",
                   version=f"yaspeed {__version__} — {REPO}")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    ui = UI(color=False if (args.no_color or args.json) else None,
            unicode_=False if args.ascii else None,
            quiet=args.quiet or args.json)
    ui.hide_cursor()
    out = {"version": __version__, "repo": REPO}

    try:
        banner(ui)

        probes = get_json(PROBES_URL)
        if args.probes_only:
            ui.show_cursor()
            print(json.dumps(probes, ensure_ascii=False, indent=2))
            return 0
        out["mid"] = probes["mid"]

        for key, url in (("ip", IP_URL), ("region", REGION_URL)):
            try:
                out[key] = get_json(url)
            except Exception as e:
                out[key] = None
                out.setdefault("warnings", []).append(f"{key}: {e}")

        if not ui.quiet:
            reg = out.get("region") or {}
            place = reg.get("name") or "—"
            ui.say(f"  {ui.dim('адрес')}   {ui.bold(str(out.get('ip') or '—'))}"
                   f"   {ui.dim('регион')} {ui.bold(place)}")
            ui.say()

        lat_probes = probes_by_lid(probes["latency"]["probes"])
        host_map = hosts_by_lid(probes["latency"]["probes"])
        lid_by_host = {v: k for k, v in host_map.items()}

        # ---------- задержка, все узлы параллельно
        results, threads = {}, []

        def ping_one(lid, url):
            results[lid] = ping_series(url, count=args.pings)

        for lid, p in lat_probes.items():
            t = threading.Thread(target=ping_one, args=(lid, p["url"]), daemon=True)
            t.start()
            threads.append(t)
        i = 0
        while any(t.is_alive() for t in threads):
            i += 1
            ui.live([f"  {ui.cyan(ui.spinner(i))}  {ui.bold('Задержка')}  "
                     f"{ui.dim('опрашиваю узлы...')}"])
            time.sleep(0.08)
        for t in threads:
            t.join(timeout=1)
        ui.live_end([])

        alive = {l: r for l, r in results.items() if r}
        if not alive:
            ui.show_cursor()
            print("Ни один узел Яндекса не ответил. Проверь подключение.",
                  file=sys.stderr)
            return 2
        out["latency"] = {l: r for l, r in alive.items()}

        for lid in sorted(alive, key=lambda l: alive[l]["min"]):
            r = alive[lid]
            name = host_map[lid][:34]
            tail = "   med {:.1f}   jitter {:.1f}".format(r["median"], r["jitter"])
            ui.say("  {}  {:<36}{}{}".format(
                ui.cyan(ui.g("•", "*")), name,
                ui.bold("{:5.1f} ms".format(r["min"])), ui.dim(tail)))

        lid = args.node if args.node != "best" else min(alive, key=lambda l: alive[l]["min"])
        if lid not in alive:
            ui.show_cursor()
            print(f"Узел {lid} недоступен.", file=sys.stderr)
            return 2
        out["lid"], out["best_latency"] = lid, alive[lid]
        idle_min = alive[lid]["min"]
        bb_url = None if args.no_bufferbloat else lat_probes[lid]["url"]
        ui.say()

        bloats = []

        # ---------- загрузка
        if not args.no_download:
            dl = probes_by_lid(probes["download"]["probes"],
                               lambda p: "timeout" not in p, lid_by_host)
            targets = ({lid: dl[lid]["url"]} if args.dl_nodes == "best"
                       else {k: v["url"] for k, v in dl.items() if k in alive})
            if targets:
                arrow = ui.g("↓", "v")
                r = measure_download(targets, args.duration, args.dl_streams,
                                     bb_url, phase_tick(ui, "Загрузка", ui.cyan, arrow))
                out["download"] = r
                phase_done(ui, "Загрузка", ui.cyan, arrow, r)
                if len(targets) > 1 and not ui.quiet:
                    for l, b in sorted(r["per_node"].items(), key=lambda x: -x[1]):
                        ui.say(f"       {ui.dim(host_map.get(l, l)[:34]):<44}"
                               f"{ui.dim(human_bits(b * 8 / args.duration).rjust(12))}")
                ll = r.get("loaded_latency")
                if ll:
                    bloats.append(ll["median"] - idle_min)

        # ---------- отдача
        if not args.no_upload:
            ul = probes_by_lid(probes["upload"]["probes"],
                               lambda p: "timeout" not in p, lid_by_host)
            ulid = lid if lid in ul else next(iter(ul), None)
            if ulid is not None:
                p = ul[ulid]
                arrow = ui.g("↑", "^")
                r = measure_upload(p.get("postUrl") or p["url"], p.get("statsUrl"),
                                   args.duration, args.ul_streams, args.ul_chunk,
                                   args.ul_mode, bb_url,
                                   phase_tick(ui, "Отдача  ", ui.mag, arrow))
                out["upload"] = r
                tag = "" if r.get("source") == "server" else ui.dim("  (счёт клиента)")
                phase_done(ui, "Отдача  ", ui.mag, arrow, r, tag)
                ll = r.get("loaded_latency")
                if ll:
                    bloats.append(ll["median"] - idle_min)

        if bloats:
            out["bufferbloat"] = max(0.0, max(bloats))

        summary(ui, out)
        if args.json:
            print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    except KeyboardInterrupt:
        ui.live_end([])
        ui.say(ui.dim("\n  прервано"))
        return 130
    except Exception as e:
        ui.live_end([])
        print(f"Ошибка: {e}", file=sys.stderr)
        print(f"Если это баг — заведи issue: {REPO}/issues", file=sys.stderr)
        return 1
    finally:
        ui.show_cursor()


if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal.default_int_handler)
    sys.exit(main())
