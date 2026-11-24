#!/usr/bin/env python3
"""Company research: fetch the logo, merge the agents' findings, build the report.

Usage:
  report.py logo  <run_dir> <domain>      (also creates the folder and starts the timer)
  report.py build <run_dir> "<Company name>" <YYYY-MM-DD> <domain> [--no-open]
  report.py wait  <run_dir>        (waits for the background run, prints its summary)
  report.py run   <run_dir> "<Company name>" <YYYY-MM-DD> <domain> [--no-open]
         (logo, then waits up to 75 s for the four agents' files, then build)

Standard library only (Python 3.8+), so it runs anywhere Claude Code runs.

logo:  finds the company's icon (its own site icons first, then DuckDuckGo's and
       Google's favicon services) and saves it as logo.<ext> in the run folder. Takes 1 to 4 seconds.
build: reads every frag-*.json the agents wrote, repairs small format slips,
       numbers each source once, keeps only recent dated news, then writes
       report.html (and data.json) and opens it in the browser.
"""

import base64
import os
import shutil
import subprocess
import html
import json
import re
import ssl
import sys
import time
import webbrowser
from datetime import date, datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit
from urllib.request import Request, urlopen

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
STATUSES = {"verified", "single_source", "not_found"}
META_KEYS = {"highlights", "not_found", "section", "key_finding", "sources"}
FRAGMENTS = ("company", "money", "news", "market")


# --------------------------------------------------------------------------- #
# Logo
# --------------------------------------------------------------------------- #

class IconParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.icons = []  # (score, href)

    def handle_starttag(self, tag, attrs):
        if tag != "link":
            return
        a = {k.lower(): (v or "") for k, v in attrs}
        rel, href = a.get("rel", "").lower(), a.get("href")
        if not href or "icon" not in rel or "mask-icon" in rel:
            return
        m = re.search(r"(\d+)x\d+", a.get("sizes", ""))
        score = int(m.group(1)) if m else 0
        if "apple-touch-icon" in rel:
            score = max(score, 180)
        if href.lower().split("?")[0].endswith(".svg") or "svg" in a.get("type", ""):
            score = max(score, 400)
        self.icons.append((score, href))


def http_get(url, timeout=4, final=None):
    req = Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urlopen(req, timeout=timeout, context=ssl.create_default_context()) as r:
        if final is not None:
            final.append(r.geturl())
        return r.read(3_000_000)


def same_site(a, b):
    """True when two hosts share a registrable name (bolt.com and help.bolt.com, not bolt.eu)."""
    a, b = host_of(a).split("."), host_of(b).split(".")
    return a[-2:] == b[-2:]


def image_type(data):
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:4] == b"GIF8":
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:4] == b"\x00\x00\x01\x00":
        return "ico"
    if b"<svg" in data[:3000].lower():
        return "svg"
    return None


def fetch_logo(run_dir, domain):
    """Also prepares the run folder and starts the timer: this is the first step to run."""
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / ".started").write_text(str(time.time()))
    for old in list(run_dir.glob("frag-*.json")) + list(run_dir.glob("logo.*")):
        if time.time() - old.stat().st_mtime > 5:  # leftovers from an earlier run today
            old.unlink()
    domain = host_of(domain)
    base = "https://" + domain
    candidates = []
    try:
        p, home = IconParser(), []
        p.feed(http_get(base + "/", final=home).decode("utf-8", errors="replace")[:400_000])
        if not home or same_site(home[-1], domain):  # only trust icons from the company's own homepage
            candidates += [urljoin(home[-1] if home else base + "/", h) for _, h in sorted(p.icons, reverse=True)]
    except Exception:
        pass
    candidates += [base + "/apple-touch-icon.png", base + "/favicon.svg", base + "/favicon.png", base + "/favicon.ico",
                   "https://icons.duckduckgo.com/ip3/%s.ico" % domain,
                   "https://www.google.com/s2/favicons?domain=%s&sz=128" % domain]
    seen = set()
    for url in candidates:
        if url in seen:
            continue
        seen.add(url)
        final = []
        try:
            data = http_get(url, timeout=3, final=final)
        except Exception:
            continue
        if url.startswith(base + "/") and final and not same_site(final[-1], domain):
            continue  # redirected to another site: not this company's icon
        ext = image_type(data)
        if not ext:
            continue
        if ext == "png" and int.from_bytes(data[16:20], "big") < 32 and "google.com/s2" not in url \
                and "duckduckgo" not in url:
            continue  # tiny favicon; try the next one
        (run_dir / ("logo." + ext)).write_bytes(data)
        (run_dir / "logo-source.txt").write_text(url + "\n")
        print("Logo saved: logo.%s from %s" % (ext, url))
        return
    print("No logo found; the report will show the company's initial.")


# --------------------------------------------------------------------------- #
# Merge and curate
# --------------------------------------------------------------------------- #

def load_json_loose(path):
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found")
    return json.loads(re.sub(r",\s*([}\]])", r"\1", text[start:end + 1]))


def host_of(url):
    url = str(url or "").strip()
    h = urlsplit(url if "://" in url else "https://" + url).netloc.lower()
    return h[4:] if h.startswith("www.") else h


def norm_url(url):
    return host_of(url) + (urlsplit(url.strip()).path.rstrip("/") or "/")


def is_fact(o):
    return isinstance(o, dict) and "value" in o and ("status" in o or "sources" in o or "src" in o or "refs" in o
                                                     or "note" in o or len(o) == 1)


EMPTY = ("", "null", "none", "unknown", "not found", "n/a", "not_found", "not disclosed")


class Curator:
    def __init__(self, own_domain):
        self.own = own_domain
        self.sources, self.by_url, self.local = [], {}, {}
        self.stats = {"verified": 0, "single_source": 0, "not_found": 0}

    def source_id(self, s):
        url = (s.get("url") or "").strip()
        if not url.startswith("http"):
            return None
        key = norm_url(url)
        tkey = host_of(url) + "|" + (s.get("title") or "").strip().lower()
        if key in self.by_url:
            return self.by_url[key]
        if (s.get("title") or "").strip() and tkey in self.by_url:  # same article, different address
            self.by_url[key] = self.by_url[tkey]
            return self.by_url[tkey]
        try:
            tier = min(max(int(s.get("tier") or 3), 1), 3)
        except (TypeError, ValueError):
            tier = 3
        host = host_of(url)
        if self.own and host.endswith(self.own):
            tier = 1
        self.sources.append({
            "id": len(self.sources) + 1, "url": url, "host": host, "tier": tier,
            "title": (s.get("title") or "").strip() or host,
            "publisher": (s.get("publisher") or "").strip() or host,
            "published": s.get("published") or None,
        })
        self.by_url[key] = len(self.sources)
        if (s.get("title") or "").strip():
            self.by_url[tkey] = len(self.sources)
        return len(self.sources)

    def fix(self, f):
        srcs = f.get("sources") or []
        if f.get("src") is not None:  # compact form: ids into the fragment's own source list
            ids = f["src"] if isinstance(f["src"], list) else [f["src"]]
            srcs = [self.local[i] for i in ids if i in self.local] + (srcs if isinstance(srcs, list) else [])
        if isinstance(srcs, dict):
            srcs = [srcs]
        refs = []
        for s in srcs:
            s = {"url": s} if isinstance(s, str) else s if isinstance(s, dict) else {}
            sid = self.source_id(s)
            if sid and sid not in refs:
                refs.append(sid)
        value = f.get("value")
        if isinstance(value, str) and value.strip().lower() in EMPTY:
            value = None
        if value in ([], {}):
            value = None
        status = str(f.get("status") or "").lower().replace(" ", "_").replace("-", "_")
        if status not in STATUSES:
            status = "single_source" if refs else "not_found"
        if value is None:
            status = "not_found"
        elif not refs or status == "not_found":
            status = "single_source"
        tiers = [self.sources[r - 1]["tier"] for r in refs]
        if status == "single_source" and 1 in tiers:
            status = "verified"  # the company's own record
        if status == "verified" and tiers and all(t == 3 for t in tiers) and re.search(r"\d", str(value)):
            status = "single_source"  # numbers resting only on weak sources are leads
        self.stats[status] += 1
        out = {"value": value, "status": status, "refs": refs if value is not None else [],
               "note": f.get("note") or ""}
        for extra in ("metric", "period"):
            if f.get(extra):
                out[extra] = str(f[extra]).strip()
        return out

    def walk(self, o):
        if is_fact(o):
            return self.fix(o)
        if isinstance(o, dict):
            return {k: self.walk(v) for k, v in o.items()}
        if isinstance(o, list):
            return [self.walk(v) for v in o]
        return o


def parse_date(t):
    t = str(t or "").strip()
    for fmt, n in (("%Y-%m-%d", 10), ("%Y-%m", 7)):
        try:
            return datetime.strptime(t[:n], fmt).date()
        except ValueError:
            pass
    return None


def merge(run_dir, own_domain, today):
    cur = Curator(own_domain)
    sections, highlights, not_found, problems = {}, [], [], []
    for name in FRAGMENTS:
        path = run_dir / ("frag-%s.json" % name)
        label = name
        if not path.exists():
            problems.append("The %s agent did not finish." % label)
            continue
        try:
            frag = load_json_loose(path)
        except Exception:
            problems.append("The %s agent's file could not be read." % label)
            continue
        cur.local = {}
        for s in frag.get("sources") or []:
            if isinstance(s, dict) and s.get("id") is not None:
                cur.local[s["id"]] = s
                try:
                    cur.local[int(s["id"])] = s
                except (TypeError, ValueError):
                    pass
        for k, v in frag.items():
            if k not in META_KEYS:
                sections[k] = cur.walk(v)
        highlights += [h.strip() for h in frag.get("highlights") or [] if isinstance(h, str) and h.strip()]
        not_found += [str(x) for x in frag.get("not_found") or [] if x]

    news = sections.get("news")
    if isinstance(news, dict):
        kept, seen = [], set()
        for it in news.get("items") or []:
            d = parse_date(it.get("date"))
            key = (it.get("headline") or "")[:50].lower()
            if not d or d > today or (today - d).days > 380 or key in seen:
                continue
            seen.add(key)
            kept.append(it)
        kept.sort(key=lambda i: str(i.get("date")), reverse=True)
        news["items"] = kept[:5]
    return {"sections": sections, "highlights": highlights[:5], "not_found": not_found,
            "sources": cur.sources, "stats": dict(cur.stats, sources=len(cur.sources)),
            "problems": problems}


# --------------------------------------------------------------------------- #
# HTML
# --------------------------------------------------------------------------- #

def esc(x):
    return html.escape(str(x), quote=True)


def val(f):
    v = f.get("value") if is_fact(f) else f
    if v is None:
        return ""
    if isinstance(v, list):
        return ", ".join(str(x) for x in v if x)
    if isinstance(v, dict):
        return ", ".join(str(x) for x in v.values() if x)
    return str(v)


def refs(f):
    """Brief research: facts are not linked to numbered sources in the text."""
    return ""
    if not is_fact(f) or not f.get("refs"):
        return ""
    one = f.get("status") == "single_source"
    return '<span class="rs">%s</span>' % "".join('<a class="ref%s" href="#s%d" title="%s">%d</a>' % (
        " one" if one else "", r, "Only one source" if one else "Verified", r) for r in f["refs"])


def favicon(host, size=32):
    return ('<img class="fav" src="https://www.google.com/s2/favicons?domain=%s&amp;sz=%d" alt="" '
            'loading="lazy" onerror="this.style.visibility=\'hidden\'">' % (esc(host), size))


SOURCE_TALK = re.compile(
    r"\b(sources?|confirm\w*|verified|unverified|per|according|listing|lists|reported|cites?|agrees?|wikipedia|crunchbase|pitchbook|sacra|reports?|says|estimates?|third-party)\b", re.I)


def tile(label, f, other=None):
    v = val(f)
    if not v:
        return ""
    note = tile_note(f, other)
    sub = '<div class="tn">%s</div>' % esc(note) if note else ""
    return '<div class="tile"><div class="tl">%s</div><div class="tv">%s</div>%s</div>' % (esc(label), esc(v), sub)


ATTRIB = re.compile(r",?\s*(?:per|according to|as reported by|reported by)\s+[^,.;]+", re.I)


def kv(rows):
    rows = [(k, f) for k, f in rows if val(f)]
    if not rows:
        return ""
    return '<dl class="kv">%s</dl>' % "".join("<dt>%s</dt><dd>%s</dd>" % (esc(k), esc(ATTRIB.sub("", val(f)).strip()))
                                             for k, f in rows)


def block(title, inner):
    return '<section><h2>%s</h2><div class="sec">%s</div></section>' % (esc(title), inner) if inner.strip() else ""


def logo_html(run_dir, name):
    mimes = {"svg": "image/svg+xml", "png": "image/png", "jpg": "image/jpeg", "webp": "image/webp",
             "gif": "image/gif", "ico": "image/x-icon"}
    for ext, mime in mimes.items():
        p = run_dir / ("logo." + ext)
        if p.exists() and p.stat().st_size > 0:
            data = base64.b64encode(p.read_bytes()).decode()
            return '<img class="logo" src="data:%s;base64,%s" alt="%s logo">' % (mime, data, esc(name))
    return '<div class="logo initial">%s</div>' % esc((name or "?")[:1].upper())


CSS = r"""
:root{--bg:#f6f5f1;--card:#fff;--ink:#1b1b19;--mut:#6d6a63;--line:#e7e4dc;--acc:#2f5d50;--accbg:#e6efeb;
--one:#9a6a00;--onebg:#fbf1db;--red:#b3261e;--redbg:#fbe5e3;--soft:#f3f1ec}
@media (prefers-color-scheme:dark){:root{--bg:#121211;--card:#1c1b19;--ink:#ecebe6;--mut:#a29e95;--line:#302e2a;
--acc:#86c4ad;--accbg:#1e302a;--one:#f0c36b;--onebg:#3a2f17;--red:#f2a19b;--redbg:#3c1f1c;--soft:#252421}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,sans-serif}
a{color:inherit}
.page{max-width:1180px;margin:0 auto;padding:28px 20px 20px;display:grid;grid-template-columns:minmax(0,1fr) 320px;gap:20px;align-items:start}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px}
main.card{padding:28px 30px}
.head{display:flex;gap:16px;align-items:center;padding-bottom:20px;border-bottom:1px solid var(--line)}
.logo{width:60px;height:60px;border-radius:14px;object-fit:contain;background:#fff;border:1px solid var(--line);padding:6px;flex:none}
.logo.initial{display:grid;place-items:center;font-size:26px;font-weight:700;color:var(--acc);background:var(--accbg)}
.head h1{margin:0;font-size:26px;line-height:1.15;letter-spacing:-.01em}
.head p{margin:4px 0 0;color:var(--mut);font-size:15px}
.meta{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.pos{margin:0 0 12px;font-size:15px;line-height:1.45}
.pill .fav{width:12px;height:12px;border-radius:2px;margin-right:5px;vertical-align:-1px}
.pill.date{color:var(--ink)}
.pill{font-size:12px;color:var(--mut);border:1px solid var(--line);border-radius:999px;padding:2px 9px;text-decoration:none}
.actions{margin-left:auto;display:flex;gap:6px;align-self:flex-start}
.btn{text-decoration:none;display:inline-block;white-space:nowrap;font:inherit;font-size:13px;border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:8px;padding:6px 10px;cursor:pointer}
.btn:hover{background:var(--soft)}
section{margin-top:26px}
h2{font-size:12.5px;text-transform:uppercase;letter-spacing:.07em;color:var(--mut);margin:0 0 10px;font-weight:650}
.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:10px}
.tile{background:var(--soft);border-radius:12px;padding:12px 14px}
.tl{font-size:12px;color:var(--mut)}
.tn{font-size:12px;color:var(--mut);margin-top:3px;line-height:1.35}
.tv{font-size:16px;font-weight:600;margin-top:2px;line-height:1.3}
.points{margin:0;padding:0;list-style:none}
.points li{position:relative;padding:7px 0 7px 18px;border-bottom:1px solid var(--line)}
.points li:last-child{border-bottom:0}
.points li:before{content:"";position:absolute;left:2px;top:16px;width:6px;height:6px;border-radius:50%;background:var(--acc)}
.prods,.rivals{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:10px}
.prod{border:1px solid var(--line);border-radius:12px;padding:11px 13px}
.prod b{display:block;font-size:14px}
.prod span{font-size:13px;color:var(--mut)}
.rival{display:flex;gap:10px;align-items:flex-start;border:1px solid var(--line);border-radius:12px;padding:11px 13px}
.rival .fav{width:22px;height:22px;border-radius:5px;margin-top:1px;flex:none}
.rival b{font-size:14px}.rival .why{font-size:13px;color:var(--mut)}
.rival a{text-decoration:none}.rival a:hover{text-decoration:underline}
.kv{display:grid;grid-template-columns:150px 1fr;gap:6px 14px;font-size:14px;margin:12px 0 0}
.kv dt{color:var(--mut)}.kv dd{margin:0}
.news{list-style:none;margin:0;padding:0}
.news li{display:grid;grid-template-columns:92px 1fr;gap:12px;padding:8px 0;border-bottom:1px solid var(--line);font-size:14px}
.news li:last-child{border-bottom:0}
.news time{color:var(--mut);font-variant-numeric:tabular-nums;font-size:13px}
.checks{list-style:none;margin:0;padding:0}
.checks li{display:grid;grid-template-columns:96px 210px 1fr;gap:10px;align-items:baseline;padding:6px 0;border-bottom:1px solid var(--line);font-size:14px}
.checks li:last-child{border-bottom:0}
.chk{font-size:11px;font-weight:700;border-radius:999px;padding:2px 8px;justify-self:start;white-space:nowrap;background:var(--soft);color:var(--mut)}
.checks .hit .chk{background:var(--onebg);color:var(--one)}
.checks .hit.high .chk{background:var(--redbg);color:var(--red)}
.checks .clear .res{color:var(--mut)}
.flags{display:flex;flex-wrap:wrap;gap:8px}
.sk{height:12px;border-radius:6px;margin:8px 0;background:linear-gradient(90deg,var(--soft) 25%,var(--line) 50%,var(--soft) 75%);
background-size:200% 100%;animation:sk 1.1s linear infinite}
@keyframes sk{from{background-position:200% 0}to{background-position:-200% 0}}
.sk-tile .sk{margin:6px 0}
.loading{display:flex;align-items:center;gap:10px;margin-top:14px;padding:10px 12px;border-radius:10px;background:var(--accbg);
color:var(--acc);font-size:14px}
.spin{width:14px;height:14px;border-radius:50%;border:2px solid var(--acc);border-right-color:transparent;animation:spin .8s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.fignotes{margin-top:12px;padding:10px 14px;border-left:3px solid var(--one);background:var(--onebg);font-size:13.5px}
.fignotes b{display:block;font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--one);margin-bottom:3px}
.fignotes ul{margin:0;padding-left:18px}.fignotes li{margin:2px 0}
.chk-mark{color:var(--one);font-weight:700;margin-left:2px}
.flaglist{list-style:none;margin:0;padding:0}
.flaglist li{display:grid;grid-template-columns:200px 1fr;gap:10px;padding:7px 10px;margin-bottom:6px;border-radius:8px;
background:var(--onebg);font-size:14px;border-left:3px solid var(--one)}
.flaglist li.high{background:var(--redbg);border-left-color:var(--red)}
.dl{position:relative}.dl summary{list-style:none}.dl summary::-webkit-details-marker{display:none}
.dl summary:after{content:" \25BE";font-size:11px}
.menu{position:absolute;right:0;top:36px;z-index:9;background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:5px;min-width:160px;box-shadow:0 8px 24px rgba(0,0,0,.12)}
.menu a{display:block;padding:7px 10px;border-radius:7px;text-decoration:none;font-size:14px}
.menu a:hover{background:var(--soft)}
.flag{font-size:13px;border-radius:10px;padding:7px 11px;background:var(--soft)}
.flag.medium{background:var(--onebg)}.flag.high{background:var(--redbg)}
.flag b{font-size:11px;text-transform:uppercase;letter-spacing:.05em;margin-right:6px}
.flag.high b{color:var(--red)}.flag.medium b{color:var(--one)}
.ref{display:inline-block;font-size:10.5px;line-height:1;min-width:16px;text-align:center;padding:3px 4px;margin-left:3px;
border-radius:5px;background:var(--soft);color:var(--mut);text-decoration:none;vertical-align:2px;font-weight:600}
.tile .ref{background:var(--card)}
.rs{white-space:nowrap}
.ref.one{background:transparent;box-shadow:inset 0 0 0 1px var(--one);color:var(--one)}
.ref:hover{background:var(--acc);color:var(--card);box-shadow:none}
.gaps{margin-top:26px;font-size:13px;color:var(--mut)}
aside{position:sticky;top:20px;display:grid;gap:14px}
aside .card{padding:16px}
aside h3{margin:0 0 10px;font-size:14px;display:flex;justify-content:space-between}
aside h3 span{color:var(--mut);font-weight:500}
.srcs{list-style:none;margin:0;padding:0}
.srcs li{display:grid;grid-template-columns:16px 1fr;gap:8px;padding:8px 0;border-top:1px solid var(--line);font-size:13px;line-height:1.35}
.srcs li:first-child{border-top:0}
.srcs .n{color:var(--mut);font-size:12px;text-align:right}
.srcs .fav{width:16px;height:16px;border-radius:3px;margin-top:1px}
.srcs a{color:var(--acc);text-decoration:underline;text-underline-offset:2px;text-decoration-thickness:1px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.srcs a:hover{text-decoration-thickness:2px}
.host{font-size:11.5px;color:var(--mut);font-family:ui-monospace,SFMono-Regular,Menlo,monospace;margin-top:2px}
.tag{font-size:10.5px;padding:0 6px;border-radius:999px;margin-left:5px;font-family:-apple-system,sans-serif;font-weight:600}
.t1{background:var(--accbg);color:var(--acc)}.t2{background:var(--soft);color:var(--mut)}
.steps{list-style:none;margin:0;padding:0;font-size:13px}
.steps li{display:flex;gap:8px;padding:3px 0}.steps li:before{content:"\2713";color:var(--acc);font-weight:700}
.legend{font-size:12px;color:var(--mut);margin-top:10px}
footer{max-width:1180px;margin:0 auto 40px;padding:0 20px;font-size:12px;color:var(--mut)}
:target{animation:hl 1.5s ease}@keyframes hl{from{background:var(--onebg)}to{background:transparent}}
@media (max-width:900px){.page{grid-template-columns:1fr}aside{position:static}}
@media (max-width:560px){.page{padding:14px 12px}main.card{padding:20px 18px}.head{flex-wrap:wrap}.actions{margin-left:0}
.kv{grid-template-columns:1fr;gap:0}.flaglist li{grid-template-columns:1fr;gap:2px}.checks li{grid-template-columns:1fr;gap:2px}.kv dt{margin-top:8px}.news li{grid-template-columns:1fr;gap:0}
.tiles{grid-template-columns:1fr 1fr}}
@page{size:A4;margin:11mm 12mm}
@media print{
:root{--bg:#fff;--card:#fff;--ink:#1b1b19;--mut:#5f5c55;--line:#e4e1d8;--acc:#1f5c4b;--soft:#f3f1ec;--onebg:#fbf1db;--redbg:#fbe5e3}
*{-webkit-print-color-adjust:exact;print-color-adjust:exact}
body{background:#fff;font-size:10.5px;line-height:1.38}
.actions{display:none}
.page{display:block;padding:0;max-width:none}
main.card,aside .card{border:0;padding:0;border-radius:0}
.head{padding-bottom:9px;gap:12px}
.logo{width:44px;height:44px;border-radius:10px;padding:4px}
.head h1{font-size:19px}.head p{font-size:11px;margin-top:2px}
.meta{margin-top:5px}.pill{font-size:9.5px;padding:1px 7px}
section{margin-top:11px;break-inside:avoid}
h2{font-size:9px;margin-bottom:5px}
.tiles{grid-template-columns:repeat(5,1fr);gap:5px}
.tile{padding:6px 8px;border-radius:7px}.tl{font-size:8.5px}.tv{font-size:11.5px}.tn{font-size:8.5px;margin-top:1px}
.points li{padding:2px 0 2px 13px;border:0}.points li:before{top:7px;width:4px;height:4px}
.pos{font-size:10.5px;margin-bottom:6px}
.prods,.rivals{grid-template-columns:repeat(3,1fr);gap:5px}
.prod,.rival{padding:6px 8px;border-radius:7px}.prod b,.rival b{font-size:10.5px}.prod span,.rival .why{font-size:9.5px}
.rival .fav{width:14px;height:14px}
.kv{font-size:10px;gap:2px 10px;margin-top:6px;grid-template-columns:110px 1fr}
.news li{padding:2px 0;font-size:10px;grid-template-columns:70px 1fr;border:0}.news time{font-size:9.5px}
.flag{font-size:9.5px;padding:4px 8px}
.gaps{margin-top:9px;font-size:9px}
aside{position:static;margin-top:0;display:block;break-before:page;page-break-before:always}
aside h3{font-size:9px;text-transform:uppercase;letter-spacing:.07em;color:var(--mut);margin-bottom:5px}
aside h3{font-size:12px!important;color:var(--ink)!important;margin-bottom:8px!important}
.srcs{columns:1}
.srcs li{break-inside:avoid;padding:5px 0;border-top:1px solid var(--line);font-size:10.5px;grid-template-columns:13px 1fr;gap:7px}
.srcs li:first-child{border-top:0}
.srcs .fav{width:12px;height:12px}
.srcs a{display:inline;-webkit-line-clamp:unset;overflow:visible}
.host{font-size:9px}
.flaglist li{grid-template-columns:150px 1fr;padding:3px 8px;margin-bottom:3px;font-size:9.5px}
.chk{font-size:8px;padding:1px 6px}
.host{font-size:8px}.tag{font-size:7.5px}
footer{margin:10px 0 0;padding:0;font-size:8px}
}
"""

THEMES = {
# ---------------------------------------------------------------- 1. Editorial: a printed briefing
"editorial": r"""
body.t-editorial{--bg:#fbfaf7;--card:#fbfaf7;--ink:#17140f;--mut:#6b655b;--line:#e2dccf;--acc:#1f3a5f;--soft:transparent;
--one:#8a5a00;--onebg:transparent;--red:#8c1d2b;--redbg:transparent;font:15.5px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,sans-serif}
body.t-editorial .page{max-width:1120px;grid-template-columns:minmax(0,1fr) 270px;gap:56px;padding-top:52px}
body.t-editorial .card,body.t-editorial main.card,body.t-editorial aside .card{background:none;border:0;border-radius:0;padding:0;box-shadow:none}
body.t-editorial aside{border-left:1px solid var(--line);padding-left:28px;top:28px}
body.t-editorial .head{border-bottom:0;padding-bottom:6px;align-items:flex-start}
body.t-editorial .logo{width:46px;height:46px;border:0;padding:0;border-radius:8px;background:#fff;margin-top:8px}
body.t-editorial .head h1{font-family:Georgia,"Iowan Old Style","Times New Roman",serif;font-size:42px;font-weight:700;letter-spacing:-.02em}
body.t-editorial .head p{font-family:Georgia,"Times New Roman",serif;font-style:italic;font-size:18.5px;color:var(--mut);margin-top:6px}
body.t-editorial .meta{gap:0;margin-top:10px}
body.t-editorial .pill{border:0;padding:0;border-radius:0;font-size:13px;color:var(--mut);background:none}
body.t-editorial .meta>*+*:before{content:"\00b7";margin:0 10px;color:#b9b2a5}
body.t-editorial .pill .fav{display:none}
body.t-editorial a.pill{color:var(--acc);text-decoration:underline;text-underline-offset:3px;text-decoration-color:#c9c2b5}
body.t-editorial .btn{border:0;background:none;padding:2px 0 2px 14px;font-size:13px;color:var(--acc);border-radius:0}
body.t-editorial .btn:hover{background:none;text-decoration:underline}
body.t-editorial section{margin-top:36px}
body.t-editorial h2{font-family:Georgia,"Times New Roman",serif;text-transform:none;letter-spacing:0;font-size:21px;color:var(--ink);
font-weight:700;border-top:1px solid var(--ink);padding-top:10px;margin-bottom:16px}
body.t-editorial .tiles{gap:0;grid-template-columns:repeat(4,1fr);row-gap:18px}
body.t-editorial .tile{background:none;border-radius:0;padding:0 18px;border-left:1px solid var(--line)}
body.t-editorial .tile:nth-child(4n+1){border-left:0;padding-left:0}
body.t-editorial .tl{font-size:11px;text-transform:uppercase;letter-spacing:.09em;color:var(--mut)}
body.t-editorial .tv{font-family:Georgia,"Times New Roman",serif;font-size:23px;font-weight:400;margin-top:3px;line-height:1.2}
body.t-editorial .tn{font-size:12.5px}
body.t-editorial .points li{border:0;padding:4px 0 4px 24px}
body.t-editorial .points li:before{content:"\2014";background:none;width:auto;height:auto;top:4px;left:0;border-radius:0;color:var(--mut)}
body.t-editorial .pos{font-family:Georgia,"Times New Roman",serif;font-style:italic;font-size:17px;margin-bottom:14px}
body.t-editorial .prods,body.t-editorial .rivals{grid-template-columns:1fr 1fr;gap:0 36px}
body.t-editorial .prod,body.t-editorial .rival{border:0;border-radius:0;padding:9px 0;border-top:1px solid var(--line);background:none}
body.t-editorial .prod b,body.t-editorial .rival b{font-size:15px}
body.t-editorial .prod span,body.t-editorial .rival .why{font-size:14px}
body.t-editorial .rival .fav{width:16px;height:16px;margin-top:4px}
body.t-editorial .kv{margin-top:16px}
body.t-editorial .news li{border-bottom:1px solid var(--line)}
body.t-editorial .flaglist li{background:none;border-radius:0;border:0;border-left:2px solid var(--red);padding:2px 0 2px 16px;margin-bottom:10px}
body.t-editorial .flaglist li b{color:var(--red)}
body.t-editorial .fignotes{background:none;border-left:2px solid var(--one);padding:2px 0 2px 16px;margin-top:20px}
body.t-editorial aside h3{font-family:Georgia,"Times New Roman",serif;font-size:17px;font-weight:700;border-top:1px solid var(--ink);padding-top:10px}
body.t-editorial .srcs li{border-top:1px solid var(--line);padding:9px 0}
body.t-editorial .srcs a{color:var(--ink);text-decoration-color:#c9c2b5}
body.t-editorial .tag{background:none;padding:0;color:var(--mut);font-weight:500}
body.t-editorial .menu{border-radius:0;box-shadow:none;border-color:var(--ink)}
@media (prefers-color-scheme:dark){body.t-editorial{--bg:#141311;--card:#141311;--ink:#ece8df;--mut:#a39d91;--line:#34312b;--acc:#9cbbe0;--red:#e38b95;--one:#e0b45c}
body.t-editorial .logo{background:#fff}}
@media (max-width:700px){body.t-editorial .tiles{grid-template-columns:1fr 1fr}body.t-editorial .tile{border:0;padding:0}
body.t-editorial .prods,body.t-editorial .rivals{grid-template-columns:1fr}}
""",
# ---------------------------------------------------------------- 2. Memo: an analyst one-pager
"memo": r"""
body.t-memo{--bg:#ffffff;--card:#ffffff;--ink:#0e1a2b;--mut:#5a6577;--line:#e3e7ed;--acc:#1d4f91;--soft:#f4f6f9;
--one:#8a5a00;--onebg:#fbf6ea;--red:#b42318;--redbg:#fdf3f2;font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,sans-serif}
body.t-memo .page{max-width:1160px;gap:48px;padding-top:44px}
body.t-memo .card,body.t-memo main.card,body.t-memo aside .card{background:none;border:0;border-radius:0;padding:0}
body.t-memo aside{border-left:1px solid var(--line);padding-left:26px}
body.t-memo .head{padding-bottom:22px}
body.t-memo .logo{width:52px;height:52px;border:1px solid var(--line);border-radius:10px;padding:6px}
body.t-memo .head h1{font-size:30px;font-weight:700;letter-spacing:-.02em;color:var(--ink)}
body.t-memo .head p{font-size:16px}
body.t-memo .meta{gap:16px;margin-top:8px}
body.t-memo .pill{border:0;padding:0;border-radius:0;font-size:13px;background:none;color:var(--mut)}
body.t-memo a.pill{color:var(--acc)}
body.t-memo .btn{border:1px solid var(--line);border-radius:6px;font-size:13px;padding:5px 11px}
body.t-memo section{display:grid;grid-template-columns:150px minmax(0,1fr);gap:28px;margin-top:0;padding:22px 0;border-top:1px solid var(--line)}
body.t-memo h2{margin:3px 0 0;font-size:13px;text-transform:none;letter-spacing:0;color:var(--acc);font-weight:650}
body.t-memo .tiles{grid-template-columns:repeat(4,1fr);gap:18px 24px}
body.t-memo .tile{background:none;border-radius:0;padding:0}
body.t-memo .tl{font-size:12px;color:var(--mut)}
body.t-memo .tv{font-size:22px;font-weight:650;letter-spacing:-.01em;font-variant-numeric:tabular-nums;margin-top:1px}
body.t-memo .points li{border:0;padding:3px 0 3px 16px}
body.t-memo .points li:before{top:12px;width:5px;height:5px;background:var(--acc)}
body.t-memo .pos{font-size:15.5px;font-weight:500;margin-bottom:12px}
body.t-memo .prods,body.t-memo .rivals{grid-template-columns:repeat(2,1fr);gap:12px 32px}
body.t-memo .prod,body.t-memo .rival{border:0;border-radius:0;padding:0;background:none}
body.t-memo .prod b,body.t-memo .rival b{font-size:14.5px}
body.t-memo .rival .fav{width:18px;height:18px}
body.t-memo .kv{margin-top:14px;grid-template-columns:140px 1fr}
body.t-memo .news li{border:0;padding:5px 0}
body.t-memo .flaglist li{background:var(--redbg);border-radius:0;border:0;border-left:3px solid var(--red);padding:7px 12px}
body.t-memo .flaglist li.medium{background:var(--onebg);border-left-color:var(--one)}
body.t-memo .flaglist li b{color:var(--ink)}
body.t-memo .fignotes{background:var(--soft);border-left:3px solid var(--acc);margin-top:16px}
body.t-memo .fignotes b{color:var(--acc)}
body.t-memo .gaps{padding-top:16px;border-top:1px solid var(--line)}
body.t-memo aside h3{font-size:13px;color:var(--acc);font-weight:650}
body.t-memo .srcs li{border-top:1px solid var(--line)}
body.t-memo .srcs a{color:var(--acc)}
body.t-memo .tag{background:var(--soft);color:var(--mut)}
@media (prefers-color-scheme:dark){body.t-memo{--bg:#0f141b;--card:#0f141b;--ink:#e8edf4;--mut:#9aa6b6;--line:#253041;--acc:#8db5ec;--soft:#17202b;
--onebg:#2a2412;--redbg:#2c1717;--red:#f0a39b;--one:#e6c071}}
@media (max-width:760px){body.t-memo section{grid-template-columns:1fr;gap:8px}body.t-memo .tiles{grid-template-columns:1fr 1fr}
body.t-memo .prods,body.t-memo .rivals{grid-template-columns:1fr}}
""",
# ---------------------------------------------------------------- 3. Data sheet: a compact fact sheet
"sheet": r"""
body.t-sheet{--bg:#ffffff;--card:#ffffff;--ink:#121212;--mut:#6a6a6a;--line:#dedede;--acc:#0a58a8;--soft:#f5f5f5;
--one:#8a5a00;--onebg:transparent;--red:#c0261c;--redbg:transparent;font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,sans-serif}
body.t-sheet .page{max-width:1180px;gap:40px;padding-top:36px}
body.t-sheet .card,body.t-sheet main.card,body.t-sheet aside .card{background:none;border:0;border-radius:0;padding:0}
body.t-sheet aside{border-left:1px solid var(--line);padding-left:22px}
body.t-sheet .head{border-bottom:3px solid var(--ink);padding-bottom:14px}
body.t-sheet .logo{width:40px;height:40px;border:0;border-radius:4px;padding:0}
body.t-sheet .head h1{font-size:26px;font-weight:800;letter-spacing:-.01em}
body.t-sheet .head p{font-size:14.5px}
body.t-sheet .meta{gap:14px;margin-top:6px}
body.t-sheet .pill{border:0;padding:0;border-radius:0;font-size:12.5px;background:none}
body.t-sheet a.pill{color:var(--acc)}
body.t-sheet .btn{border:1px solid var(--ink);border-radius:0;font-size:12px;padding:4px 10px;text-transform:uppercase;letter-spacing:.05em}
body.t-sheet section{margin-top:22px}
body.t-sheet h2{font-size:11px;letter-spacing:.11em;color:var(--ink);font-weight:700;border-bottom:1px solid var(--ink);padding-bottom:4px;margin-bottom:6px}
body.t-sheet .tiles{grid-template-columns:1fr 1fr;gap:0 36px}
body.t-sheet .tile{background:none;border-radius:0;padding:6px 0;border-bottom:1px solid var(--line);display:grid;grid-template-columns:1fr auto;column-gap:12px;align-items:baseline}
body.t-sheet .tl{font-size:13px;color:var(--mut)}
body.t-sheet .tv{font-size:14px;font-weight:650;text-align:right;margin:0;font-variant-numeric:tabular-nums}
body.t-sheet .tn{grid-column:1/-1;font-size:11.5px;margin-top:0}
body.t-sheet .points li{border-bottom:1px solid var(--line);padding:5px 0 5px 14px}
body.t-sheet .points li:before{border-radius:0;width:4px;height:4px;top:14px;background:var(--ink)}
body.t-sheet .pos{font-size:14px;margin:4px 0 6px;color:var(--mut)}
body.t-sheet .prods,body.t-sheet .rivals{grid-template-columns:1fr;gap:0}
body.t-sheet .prod{border:0;border-bottom:1px solid var(--line);border-radius:0;padding:6px 0;display:grid;grid-template-columns:200px 1fr;gap:14px;background:none}
body.t-sheet .prod b{font-size:13.5px}body.t-sheet .prod span{font-size:13.5px}
body.t-sheet .rival{border:0;border-bottom:1px solid var(--line);border-radius:0;padding:6px 0;background:none}
body.t-sheet .rival>div{display:grid;grid-template-columns:186px 1fr;gap:14px;width:100%}
body.t-sheet .rival b{font-size:13.5px}body.t-sheet .rival .why{font-size:13.5px}
body.t-sheet .rival .fav{width:14px;height:14px;margin-top:3px}
body.t-sheet .kv{margin-top:6px;font-size:13.5px}
body.t-sheet .news li{padding:5px 0;font-size:13.5px}
body.t-sheet .flaglist li{background:none;border-radius:0;border:0;border-bottom:1px solid var(--line);padding:6px 0;margin:0}
body.t-sheet .flaglist li b{color:var(--red)}
body.t-sheet .fignotes{background:none;border:0;border-left:2px solid var(--one);padding:2px 0 2px 12px;margin-top:10px;font-size:12.5px}
body.t-sheet aside h3{font-size:11px;letter-spacing:.11em;text-transform:uppercase;border-bottom:1px solid var(--ink);padding-bottom:4px}
body.t-sheet .srcs li{border-top:0;border-bottom:1px solid var(--line);padding:6px 0}
body.t-sheet .srcs a{color:var(--acc)}
body.t-sheet .tag{background:none;padding:0;color:var(--mut);font-weight:600}
@media (prefers-color-scheme:dark){body.t-sheet{--bg:#111;--card:#111;--ink:#eee;--mut:#a3a3a3;--line:#2e2e2e;--acc:#7fb4ef;--red:#f19a92;--one:#e6c071}}
@media (max-width:700px){body.t-sheet .tiles{grid-template-columns:1fr}body.t-sheet .prod,body.t-sheet .rival>div{grid-template-columns:1fr;gap:0}}
""",
# ---------------------------------------------------------------- 4. Square: original colours, crisp square blocks
"square": r"""
body.t-square .card{border-radius:0}
body.t-square main.card{padding:34px 38px;border:1px solid var(--line)}
body.t-square aside .card{border:1px solid var(--line);padding:18px 18px 10px}
body.t-square .head{padding-bottom:22px}
body.t-square .logo{border-radius:6px}
body.t-square .head h1{font-size:30px;letter-spacing:-.02em}
body.t-square .meta{gap:0;margin-top:9px}
body.t-square .pill{border:0;border-radius:0;padding:0;background:none;font-size:13px}
body.t-square .meta>*+*:before{content:"\00b7";margin:0 10px;color:#c4beb2}
body.t-square a.pill{color:var(--acc)}
body.t-square .btn{border-radius:0;border-color:var(--line);font-size:13px}
body.t-square .menu{border-radius:0;box-shadow:0 6px 18px rgba(0,0,0,.08)}
body.t-square .menu a{border-radius:0}
body.t-square section{margin-top:30px}
body.t-square h2{color:var(--acc);font-size:11.5px;letter-spacing:.1em;padding-bottom:7px;border-bottom:1px solid var(--line);margin-bottom:12px}
body.t-square .tiles{gap:2px;grid-template-columns:repeat(4,1fr)}
body.t-square .tile{border-radius:0;padding:14px 16px 13px}
body.t-square .tv{font-size:18px}
body.t-square .points li{border-bottom:1px solid var(--line)}
body.t-square .points li:before{border-radius:0;width:5px;height:5px;top:17px}
body.t-square .prods,body.t-square .rivals{gap:2px}
body.t-square .prod,body.t-square .rival{border:0;border-radius:0;background:var(--soft);border-top:2px solid var(--acc);padding:12px 14px}
body.t-square .rival .fav{border-radius:3px}
body.t-square .kv{margin-top:14px}
body.t-square .flaglist li{border-radius:0;border-left:0;padding:9px 14px;margin-bottom:2px}
body.t-square .flaglist li b{color:var(--one)}
body.t-square .flaglist li.high b{color:var(--red)}
body.t-square .srcs .tag{border-radius:0}
@media (max-width:700px){body.t-square .tiles{grid-template-columns:1fr 1fr}}
""",
# ---------------------------------------------------------------- 5. Band: original colours, open layout, one tinted band
"band": r"""
body.t-band .page{gap:44px;padding-top:40px}
body.t-band .card{border-radius:0}
body.t-band main.card{border:0;padding:36px 40px}
body.t-band aside .card{background:none;border:0;padding:6px 0 0}
body.t-band .head{border-bottom:0;padding-bottom:18px}
body.t-band .logo{border-radius:8px}
body.t-band .head h1{font-size:32px;letter-spacing:-.02em}
body.t-band .head p{font-size:16px}
body.t-band .meta{gap:0;margin-top:9px}
body.t-band .pill{border:0;border-radius:0;padding:0;background:none;font-size:13px}
body.t-band .meta>*+*:before{content:"\00b7";margin:0 10px;color:#c4beb2}
body.t-band a.pill{color:var(--acc)}
body.t-band .btn{border:0;background:none;color:var(--acc);padding:4px 0 4px 14px;font-size:13px}
body.t-band .btn:hover{background:none;text-decoration:underline}
body.t-band .menu{border-radius:0}
body.t-band section{margin-top:32px}
body.t-band{--rule:#dcdcdc;--band:#f2f2f2}
body.t-band h2,body.t-band aside h3{display:flex;align-items:center;justify-content:flex-start;gap:10px;text-transform:uppercase;letter-spacing:.09em;
font-size:15px;color:var(--ink);font-weight:700;margin:0 0 14px}
body.t-band h2:before,body.t-band aside h3:before{content:none}
body.t-band .tiles{gap:0;grid-template-columns:repeat(4,1fr);background:var(--band);padding:16px 0;row-gap:16px}
body.t-band .tile{background:none;border-radius:0;padding:0 20px;border-left:1px solid var(--rule)}
body.t-band .tile:nth-child(4n+1){border-left:0}
body.t-band .tv{font-size:19px}
body.t-band .points li{border-bottom:1px solid var(--line)}
body.t-band .prods,body.t-band .rivals{grid-template-columns:1fr 1fr;gap:0 32px}
body.t-band .prod,body.t-band .rival{border:0;border-radius:0;border-top:1px solid var(--line);padding:10px 0;background:none}
body.t-band .kv{margin-top:14px}
body.t-band .flaglist li{background:none;border-radius:0;border:0;border-top:1px solid var(--line);padding:9px 0;margin:0}
body.t-band .flaglist li b{color:var(--one);display:flex;align-items:center;gap:8px}
body.t-band .flaglist li b:before{content:"";width:7px;height:7px;border-radius:50%;background:var(--one);flex:none}
body.t-band .flaglist li.high b{color:var(--red)}
body.t-band .flaglist li.high b:before{background:var(--red)}

body.t-band .srcs li{border-top:1px solid var(--line)}
@media (prefers-color-scheme:dark){body.t-band{--rule:#3a3a3a;--band:#262626}}
body.t-band .srcs .tag{border-radius:0}
@media print{body.t-band .page{padding:0}body.t-band main.card{padding:0}body.t-band section{margin-top:13px}
body.t-band h2,body.t-band aside h3{font-size:11.5px;margin-bottom:6px;gap:7px}

body.t-band .head{padding-bottom:8px}body.t-band .head h1{font-size:21px}
body.t-band .tiles{padding:7px 0;row-gap:7px}body.t-band .tile{padding:0 10px}body.t-band .tv{font-size:12.5px}
body.t-band .prod,body.t-band .rival{padding:4px 0}body.t-band .flaglist li{padding:3px 0}
body.t-band .points li{border:0}body.t-band .kv{margin-top:6px}}
@media (max-width:700px){body.t-band .tiles{grid-template-columns:1fr 1fr}body.t-band .tile{border:0}
body.t-band .meta{gap:4px 14px}body.t-band .meta>*+*:before{display:none}
body.t-band .prods,body.t-band .rivals{grid-template-columns:1fr}}
""",
}
DEFAULT_THEME = os.environ.get("BRIEF_RESEARCH_THEME", "band")


JS = r"""<script>
function copyBrief(btn){var t=document.querySelector('main').innerText.replace(/\n{3,}/g,'\n\n');
navigator.clipboard.writeText(t).then(function(){btn.textContent='Copied';setTimeout(function(){btn.textContent='Copy'},1500)});}
</script>"""


FLAG_CHECKS = [
    ("layoffs", "Layoffs", r"layoff|lay off|job cut|redundanc|headcount cut|restructur"),
    ("founder_departure", "Founder departures", r"founder|co-founder|steps? down|stepped down|leaves|left the|depart|resign|ousted"),
    ("legal_regulatory", "Lawsuits or regulatory trouble",
     r"lawsuit|sued|suit|court|regulat|fine|fined|probe|investigat|antitrust|settle|penalt|licen[cs]e|sanction|watchdog|ban"),
]


NEWS_SIGNALS = {  # stricter patterns for scanning news headlines
    "layoffs": r"layoff|lay off|job cuts?|cuts? (?:about |some |nearly |roughly )?[\d,.%]+.*(?:staff|jobs|workforce|employees)|(?:staff|workforce|jobs) cuts?|redundanc",
    "founder_departure": r"(?:founder|co-founder|ceo|chief executive)\b.*\b(?:steps? down|stepping down|leaves|left|depart|resign|ousted|exits?)",
    "legal_regulatory": r"lawsuit|\bsued\b|\bsues\b|court|\bfined?\b|fines\b|probe|investigat|antitrust|settlement|penalt|sanction",
}


def months_since(d, today):
    return (today.year - d.year) * 12 + (today.month - d.month)


FLAG_LABELS = {"layoffs": "Layoffs", "founder_departure": "Founder departure",
               "legal_regulatory": "Lawsuit or regulatory trouble", "down_round": "Down round",
               "raise_gap": "Long gap since last raise", "other": "Other"}


def same_story(a, b):
    """True when two lines describe the same event: a shared money amount or mostly the same words."""
    am = {raw.lower().replace(" ", "") for _, raw in parse_amounts(a)}
    bm = {raw.lower().replace(" ", "") for _, raw in parse_amounts(b)}
    if am & bm:
        return True
    wa = {w for w in re.findall(r"[a-z]{4,}", a.lower())}
    wb = {w for w in re.findall(r"[a-z]{4,}", b.lower())}
    return bool(wa and wb) and len(wa & wb) / min(len(wa), len(wb)) >= 0.5


def found_flags(nw, m, listed, run_date):
    """Only red flags that were actually found: the five checks plus anything else serious."""
    today = parse_date(run_date) or date.today()
    flags = [f for f in nw.get("red_flags") or [] if isinstance(f, dict) and f.get("flag")
             and not str(f["flag"]).lower().startswith("no ")]
    # Cross-check: a red flag the news agent filed only as a news item still counts.
    for item in nw.get("items") or []:
        h = str(item.get("headline") or "")
        for key, pat in NEWS_SIGNALS.items():
            if re.search(pat, h, re.I) and not any(same_story(h, str(f.get("flag"))) for f in flags):
                when = str(item.get("date") or "")[:7]
                flags.append({"category": key, "flag": h + (" (%s)" % when if when else ""), "severity": "medium"})
                break
    out, used = [], set()
    for key, label, pattern in FLAG_CHECKS:
        hits = [i for i, f in enumerate(flags) if i not in used and (
            str(f.get("category") or "").lower() == key or
            (not f.get("category") and re.search(pattern, str(f.get("flag")), re.I)))]
        used.update(hits)
        if hits:
            sev = "high" if any(flags[i].get("severity") == "high" for i in hits) else "medium"
            out.append({"label": FLAG_LABELS[key], "text": "; ".join(str(flags[i]["flag"]) for i in hits), "sev": sev})
    if not listed.get("is_listed"):
        down = val(m.get("down_round"))
        if down and not re.match(r"^\s*no\b", down, re.I):
            out.append({"label": FLAG_LABELS["down_round"], "text": down, "sev": "high"})
        last = parse_date(m.get("last_round_date"))
        if last and months_since(last, today) >= 18:
            out.append({"label": FLAG_LABELS["raise_gap"], "sev": "medium", "text": "%d months since the last raise (%s %d)" % (
                months_since(last, today), last.strftime("%b"), last.year)})
    for i, f in enumerate(flags):
        if i not in used:
            out.append({"label": FLAG_LABELS["other"], "text": str(f["flag"]),
                        "sev": "high" if f.get("severity") == "high" else "medium"})
    return out


def red_flag_html(found):
    return '<ul class="flaglist">%s</ul>' % "".join(
        '<li class="%s"><b>%s</b><span>%s</span></li>' % (esc(f["sev"]), esc(f["label"]), esc(f["text"]))
        for f in found) if found else ""


def plain(h):
    """A brief: drop "per X" / "according to X" attributions from the text."""
    h = re.sub(r",?\s*(?:per|according to|as reported by|reported by)\s+[^,.;]+", "", str(h), flags=re.I)
    return h.strip().rstrip(",;")


def stock_of(m):
    listed = m.get("listed") if isinstance(m.get("listed"), dict) else {}
    if listed.get("is_listed") and (listed.get("ticker") or listed.get("exchange")):
        return ": ".join(x for x in (listed.get("exchange"), listed.get("ticker")) if x)
    return ""


REV_LABEL = {"revenue": "Revenue", "net revenue": "Net revenue", "arr": "ARR", "run-rate": "Revenue run-rate",
             "gmv": "GMV", "tpv": "Payment volume", "estimate": "Revenue (estimate)", "unclear": "Revenue"}
PROFIT_LABEL = {"net income": "Net income", "pre-tax profit": "Pre-tax profit", "operating profit": "Operating profit",
                "ebitda": "EBITDA", "unclear": "Profit"}
METRIC_NOTE = {
    "arr": "ARR is annual recurring revenue: a run-rate of subscriptions, not reported (audited) revenue.",
    "run-rate": "Revenue run-rate is a recent month or quarter scaled up to a year, not reported revenue.",
    "gmv": "GMV is the value of goods sold through the platform, not the company's own revenue.",
    "tpv": "Payment volume is the total value processed, not the company's own revenue.",
    "estimate": "Revenue is a third-party estimate, not a figure reported by the company.",
    "unclear": "The sources don't say whether the revenue figure is reported revenue, ARR or an estimate.",
}
PROFIT_NOTE = {
    "ebitda": "EBITDA leaves out interest, tax, depreciation and amortisation, so it is not net profit.",
    "pre-tax profit": "Profit is shown before tax.",
    "operating profit": "Operating profit is before interest and tax, so it is not net profit.",
    "unclear": "The sources don't say which profit measure this is (net, pre-tax or EBITDA).",
}
FX_TO_USD = {"$": 1.0, "us$": 1.0, "usd": 1.0, "£": 1.27, "gbp": 1.27, "€": 1.08, "eur": 1.08}
UNIT = {"trillion": 1e12, "tn": 1e12, "t": 1e12, "billion": 1e9, "bn": 1e9, "b": 1e9,
        "million": 1e6, "mn": 1e6, "m": 1e6, "thousand": 1e3, "k": 1e3}
AMOUNT_RE = re.compile(r"(US\$|\$|£|€)\s?(\d+(?:[.,]\d+)*)\s?(trillion|billion|million|thousand|tn|bn|mn|[tbmk])?\b", re.I)
FAMILY = {
    "revenue": r"revenue|sales|turnover|\barr\b|run-?rate|annual recurring|gmv|payment volume",
    "profit": r"profit|net income|earnings|ebitda|operating income|loss",
    "valuation": r"valuation|valued|market cap",
}
PART_PERIOD = re.compile(r"\b(q[1-4]|quarter|quarterly|h[12]|half[- ]year|first half|second half|six months|monthly|month)\b", re.I)


def parse_amounts(text):
    out = []
    for m in AMOUNT_RE.finditer(str(text)):
        cur, num, unit = m.group(1).lower(), m.group(2).replace(",", ""), (m.group(3) or "").lower()
        try:
            v = float(num) * UNIT.get(unit, 1.0) * FX_TO_USD.get(cur, 1.0)
        except ValueError:
            continue
        if v >= 1e5:  # ignore small numbers such as prices
            out.append((v, m.group(0).strip()))
    return out


def metric_of(f):
    return str((f or {}).get("metric") or "").strip().lower() if is_fact(f) else ""


def figure_check(c, m, mk, nw, data):
    """Plain-English notes on what the figures mean, plus places where agents disagree.
    Returns (notes, flagged_labels)."""
    notes, flagged = [], {}
    rev, prof, valn = m.get("revenue"), m.get("profit"), m.get("market_cap_or_valuation")
    rm, pm = metric_of(rev), metric_of(prof)
    if val(rev):
        if rm in METRIC_NOTE:
            notes.append(METRIC_NOTE[rm])
        elif not rm:
            notes.append(METRIC_NOTE["unclear"])
    if val(prof) and pm in PROFIT_NOTE:
        notes.append(PROFIT_NOTE[pm])
    # Statements from the other agents that mention a money figure
    statements = [("a key point", h) for h in data.get("highlights") or []]
    statements += [("the news", i.get("headline")) for i in nw.get("items") or [] if isinstance(i, dict)]
    statements += [("the market section", val(mk.get("position")))]
    tiles = {"revenue": (rev, revenue_label(rev)), "profit": (prof, profit_label(prof)),
             "valuation": (valn, "Valuation")}
    for fam, (fact, label) in tiles.items():
        mine = parse_amounts(val(fact))
        if not mine:
            continue
        mine_v, mine_raw = mine[0]
        for where, s in statements:
            s = str(s or "")
            if not s or not re.search(FAMILY[fam], s, re.I) or PART_PERIOD.search(s):
                continue
            if fam == "revenue" and re.search(r"retention|growth rate|margin", s, re.I):
                continue
            for v, raw in parse_amounts(s)[:1]:
                if abs(v - mine_v) / max(v, mine_v) > 0.2:
                    period = (fact or {}).get("period") if is_fact(fact) else ""
                    notes.append("%s figures differ: the tile shows %s %s%s, but %s says %s. They may measure "
                                 "different things or periods, so check before using either." % (
                                     fam.capitalize(), label, mine_raw, " (%s)" % period if period else "", where, raw))
                    flagged.setdefault(label, raw)
    seen, out = set(), []
    for n in notes:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out, flagged


def revenue_label(f):
    return REV_LABEL.get(metric_of(f), "Revenue")


def profit_label(f):
    return PROFIT_LABEL.get(metric_of(f), "Profit")


def glance_items(c, m):
    stock = stock_of(m)
    items = [("Founded", c.get("founded")), ("Headquarters", c.get("hq")), ("CEO", c.get("ceo")),
             ("Employees", c.get("employees")), (revenue_label(m.get("revenue")), m.get("revenue")),
             (profit_label(m.get("profit")), m.get("profit"))]
    if stock:
        items += [("Stock", stock), ("Market cap", m.get("market_cap_or_valuation"))]
    else:
        items += [("Total raised", m.get("total_funding")), ("Valuation", m.get("market_cap_or_valuation")),
                  ("Last round", m.get("last_round"))]
    return [(k, f) for k, f in items if val(f)]


def tile_note(f, other=None):
    if not is_fact(f):
        return ""
    note = f.get("note") or ""
    if note and (len(note) > 60 or SOURCE_TALK.search(note)):
        note = ""
    period = f.get("period") or ""
    if period and period.lower() not in note.lower():
        note = period + (", " + note if note else "")
    if str(f.get("metric") or "").lower() == "unclear":
        note = (note + ", " if note else "") + "type not stated"
    if other:
        note = (note + " \u00b7 " if note else "") + "Other sources: " + other
    return note


def edge_rows(mk):
    rows = []
    for e in mk.get("edge") or []:
        if isinstance(e, dict) and e.get("claim") and "not found" not in e["claim"].lower():
            f = e.get("fact") if is_fact(e.get("fact")) else {}
            if f.get("status") != "not_found":
                rows.append(("Says sets it apart", e["claim"]))
    return rows


def not_found_list(data, c, m, mk):
    stock = stock_of(m)
    shown = {"revenue": m.get("revenue"), "profit|net income": m.get("profit"), "ceo": c.get("ceo"),
             "founded|founding": c.get("founded"), "employee|headcount": c.get("employees"),
             "headquarters|hq": c.get("hq"), "valuation|market cap": m.get("market_cap_or_valuation"),
             "linkedin": c.get("linkedin_url"), "business model": c.get("business_model")}
    answered = [k for k, f in shown.items() if val(f)]
    rival_names = [str(r.get("name") or "").lower() for r in mk.get("competitors") or [] if isinstance(r, dict)]
    nf = set()
    for x in data["not_found"]:
        low = x.lower()
        if low.startswith("no ") or re.search(r"layoff|lawsuit|fine|breach|investigation|red flag|down round|linkedin", low):
            continue
        if any(re.search(r"\b(%s)" % k, low) for k in answered) or any(n and low.startswith(n) for n in rival_names):
            continue
        x = re.sub(r"\s*\(.*?\)", "", x.replace("_", " ")).strip().rstrip(".")
        if not x or len(x) > 60 or SOURCE_TALK.search(x) or re.search(r"consensus|audited|independent", x, re.I) \
                or (stock and re.search(r"fund|round|investor|valuation", x, re.I)) \
                or (not stock and re.search(r"stock|ticker|ipo\b|exchange", x, re.I)):
            continue
        nf.add(x[0].upper() + x[1:])
    return sorted(nf, key=str.lower)[:8]


def linkedin_url(c, name):
    li = c.get("linkedin_url") if isinstance(c.get("linkedin_url"), str) else ""
    if "linkedin.com/company" in li.lower():
        return li, True
    return "https://www.linkedin.com/search/results/companies/?keywords=%s" % quote(name), False


def nice_date(run_date):
    d = parse_date(run_date)
    return "%d %s" % (d.day, d.strftime("%B %Y")) if d else run_date


def download_menu(exports):
    """One Download button with a small menu: PDF, Word, HTML."""
    items = []
    if exports.get("pdf"):
        items.append('<a href="%s" download>PDF</a>' % esc(quote(exports["pdf"])))
    else:
        items.append('<a href="#" onclick="window.print();return false">PDF (print)</a>')
    if exports.get("docx"):
        items.append('<a href="%s" download>Word (.docx)</a>' % esc(quote(exports["docx"])))
    if exports.get("html"):
        items.append('<a href="report.html" download="%s">HTML</a>' % esc(exports["html"]))
    return ('<details class="dl"><summary class="btn">Download</summary><div class="menu">%s</div></details>'
            % "".join(items))


def skeleton(kind, n=3):
    if kind == "tiles":
        return "".join('<div class="tile sk-tile"><div class="sk" style="width:50%"></div>'
                       '<div class="sk" style="width:80%;height:18px"></div></div>' for _ in range(n))
    if kind == "cards":
        return '<div class="prods">%s</div>' % "".join(
            '<div class="prod"><div class="sk" style="width:60%"></div><div class="sk"></div></div>' for _ in range(n))
    return "".join('<div class="sk" style="width:%d%%"></div>' % w for w in (92, 78, 85)[:n])


def render(run_dir, name, run_date, domain, data, elapsed, exports=None, pending=(), theme=None):
    s = data["sections"]
    c, m = s.get("company") or {}, s.get("money") or {}
    mk, nw = s.get("market") or {}, s.get("news") or {}

    listed = m.get("listed") if isinstance(m.get("listed"), dict) else {}
    fig_notes, fig_flagged = figure_check(c, m, mk, nw, data)
    glance = "".join(tile(k, f, fig_flagged.get(k)) for k, f in glance_items(c, m))
    if "company" in pending or "money" in pending:
        glance += skeleton("tiles", 3 if glance else 6)
    points = "".join("<li>%s</li>" % esc(plain(h)) for h in data["highlights"])

    prods = "".join('<div class="prod"><b>%s%s</b><span>%s</span></div>' % (
        esc(p.get("name")), refs(p.get("fact")), esc(p.get("what") or ""))
        for p in c.get("products") or [] if isinstance(p, dict) and p.get("name"))
    positioning = val(c.get("positioning"))
    sell = ('<p class="pos">%s</p>' % esc(positioning) if positioning else "") + \
        ('<div class="prods">%s</div>' % prods if prods else "") + kv(
        [("Business model", c.get("business_model")), ("Customers", c.get("customers")),
         ("Investors", m.get("investors"))])

    rivals = ""
    for r in mk.get("competitors") or []:
        if not isinstance(r, dict) or not r.get("name"):
            continue
        host = host_of(r.get("website")) if r.get("website") else ""
        nm = esc(r["name"])
        if host:
            nm = '<a href="https://%s" target="_blank" rel="noopener">%s</a>' % (esc(host), nm)
        rivals += '<div class="rival">%s<div><b>%s</b>%s<div class="why">%s</div></div></div>' % (
            favicon(host, 64) if host else "", nm, refs(r.get("fact")), esc(r.get("why") or ""))
    if "company" in pending:
        points = ""
        sell = skeleton("cards")
    edge = edge_rows(mk)
    compete = ('<div class="rivals">%s</div>' % rivals if rivals else "") + kv(
        [("Category", mk.get("category")), ("Position", mk.get("position"))] + edge)

    items = "".join('<li><time>%s</time><div>%s%s</div></li>' % (
        esc(i.get("date") or ""), esc(i.get("headline") or ""), refs(i.get("fact"))) for i in nw.get("items") or [])
    news = '<ul class="news">%s</ul>' % items if items else ""
    if "market" in pending:
        compete = skeleton("cards")
    if "news" in pending:
        news = skeleton("lines")

    watch = red_flag_html(found_flags(nw, m, listed, run_date))
    if "news" in pending or "money" in pending:
        watch = skeleton("lines", 2)

    nf = [] if pending else not_found_list(data, c, m, mk)
    gaps = ""
    if nf or (data["problems"] and not pending):
        gaps = '<div class="gaps">%s%s</div>' % (
            ("<b>Not found:</b> %s. " % esc("; ".join(nf[:8]))) if nf else "", esc(" ".join(data["problems"])))

    srcs = "".join(
        '<li>%s<div><a href="%s" target="_blank" rel="noopener">%s</a>'
        '<div class="host">%s%s</div></div></li>' % (
            favicon(x["host"]), esc(x["url"]), esc(x["title"]), esc(x["host"]),
            '<span class="tag t1">Official</span>' if x["tier"] == 1 else
            ('<span class="tag t2">Press</span>' if x["tier"] == 2 else "")) for x in data["sources"])

    st = data["stats"]
    one_liner = val(c.get("one_liner")) or ("Researching\u2026" if "company" in pending else "")
    li, li_official = linkedin_url(c, name)
    li_html = '<a class="pill" href="%s" target="_blank" rel="noopener"%s>%sLinkedIn</a>' % (
        esc(li), "" if li_official else ' title="Official page not found; opens a LinkedIn search"',
        favicon("linkedin.com"))
    head = ('<div class="head">%s<div><h1>%s</h1><p>%s</p><div class="meta">'
            '<a class="pill" href="https://%s" target="_blank" rel="noopener">%s%s</a>%s'
            '<span class="pill date">Researched %s</span></div></div>'
            '<div class="actions"><button class="btn" onclick="copyBrief(this)">Copy</button>'
            '%s</div></div>') % (
        logo_html(run_dir, name), esc(name), esc(one_liner), esc(domain), favicon(domain), esc(domain),
        li_html, esc(nice_date(run_date)), download_menu(exports or {}))
    if pending:
        done = 4 - len(pending)
        head = head.replace('<div class="actions">', '<div class="actions" style="display:none">')
        head += ('<div class="loading"><span class="spin"></span>Researching %s: %d of 4 parts in. '
                 'This page fills in as each part arrives.</div>' % (esc(name), done))
    main = '<main class="card">%s%s%s%s%s%s%s%s</main>' % (
        head,
        block("At a glance", '<div class="tiles">%s</div>' % glance if glance else ""),
        block("Key points", '<ul class="points">%s</ul>' % points if points else (
            skeleton("lines") if "company" in pending else "")),
        block("What they sell", sell),
        block("Main Competitors", compete),
        block("Recent news", news),
        block("Red flags", watch),
        gaps)
    if pending and len(data["sources"]) < 3:
        srcs += "".join('<li><span></span><div><div class="sk" style="width:%d%%"></div></div></li>' % w for w in (80, 65, 72))
    aside = '<aside><div class="card"><h3>Sources</h3><ul class="srcs">%s</ul></div></aside>' % srcs

    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>%s: company brief</title>%s<style>%s%s</style></head><body class="t-%s">'
            '<div class="page">%s%s</div><footer>Built with Company Research for Claude Code from the '
            'public sources listed. Gathered by AI: check anything important at the source.</footer>%s</body></html>') % (
        esc(name), '<meta http-equiv="refresh" content="1">' if pending else "", CSS,
        THEMES.get(theme or DEFAULT_THEME, ""), esc(theme or DEFAULT_THEME), main, aside, JS)


def _x(s):
    return html.escape(str(s), quote=False)


def export_docx(path, run_dir, name, run_date, domain, data):
    """A clean, editable Word version of the brief: page 1 the brief, then sources as links."""
    import zipfile
    s = data["sections"]
    c, m, mk, nw = (s.get(k) or {} for k in ("company", "money", "market", "news"))
    listed = m.get("listed") if isinstance(m.get("listed"), dict) else {}
    rels, body = [], []

    def link(url, text, size=None):
        rid = "rId%d" % (len(rels) + 10)
        rels.append('<Relationship Id="%s" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
                    'hyperlink" Target="%s" TargetMode="External"/>' % (rid, html.escape(url, quote=True)))
        sz = '<w:sz w:val="%d"/>' % size if size else ""
        return ('<w:hyperlink r:id="%s"><w:r><w:rPr><w:color w:val="1F5C4B"/><w:u w:val="single"/>%s</w:rPr>'
                '<w:t xml:space="preserve">%s</w:t></w:r></w:hyperlink>' % (rid, sz, _x(text)))

    def run(text, bold=False, size=None, color=None):
        pr = ("<w:b/>" if bold else "") + ('<w:sz w:val="%d"/>' % size if size else "") + \
            ('<w:color w:val="%s"/>' % color if color else "")
        return '<w:r><w:rPr>%s</w:rPr><w:t xml:space="preserve">%s</w:t></w:r>' % (pr, _x(text))

    def para(*runs, after=80, style=None, keep=False):
        ppr = ('<w:pStyle w:val="%s"/>' % style if style else "") + ("<w:keepNext/>" if keep else "") + \
            '<w:spacing w:after="%d"/>' % after
        body.append("<w:p><w:pPr>%s</w:pPr>%s</w:p>" % (ppr, "".join(runs)))

    def heading(text):
        para(run(text.upper(), bold=True, size=22, color="1B1B19"), after=80, keep=True)

    def bullet(*runs):
        body.append('<w:p><w:pPr><w:spacing w:after="40"/><w:ind w:left="284" w:hanging="200"/></w:pPr>%s%s</w:p>'
                    % (run("•  ", color="2F5D50"), "".join(runs)))

    def table(rows, widths):
        grid = "".join('<w:gridCol w:w="%d"/>' % w for w in widths)
        trs = ""
        for row in rows:
            tcs = ""
            for (cell_runs, w) in zip(row, widths):
                tcs += ('<w:tc><w:tcPr><w:tcW w:w="%d" w:type="dxa"/><w:shd w:val="clear" w:color="auto" w:fill="F2F2F2"/>'
                        '<w:tcMar><w:top w:w="60" w:type="dxa"/><w:bottom w:w="60" w:type="dxa"/></w:tcMar></w:tcPr>'
                        '<w:p><w:pPr><w:spacing w:after="0"/></w:pPr>%s</w:p></w:tc>' % (w, cell_runs))
            trs += "<w:tr>%s</w:tr>" % tcs
        body.append('<w:tbl><w:tblPr><w:tblW w:w="%d" w:type="dxa"/><w:tblBorders><w:insideH w:val="single" w:sz="8" '
                    'w:color="FFFFFF"/><w:insideV w:val="single" w:sz="8" w:color="FFFFFF"/></w:tblBorders>'
                    '</w:tblPr><w:tblGrid>%s</w:tblGrid>%s</w:tbl>' % (sum(widths), grid, trs))
        para(after=60)

    # Header
    para(run(name, bold=True, size=40), after=40)
    if val(c.get("one_liner")):
        para(run(val(c.get("one_liner")), size=22, color="6D6A63"), after=60)
    li, _ = linkedin_url(c, name)
    para(link("https://" + domain, domain, 18), run("   ·   ", size=18, color="6D6A63"), link(li, "LinkedIn", 18),
         run("   ·   Researched %s" % nice_date(run_date), size=18, color="6D6A63"), after=200)

    # At a glance: 3 tiles per row
    tiles = []
    others = figure_check(c, m, mk, nw, data)[1]
    for label, f in glance_items(c, m):
        note = tile_note(f, others.get(label))
        tiles.append(run(label, size=15, color="6D6A63") + '<w:r><w:br/></w:r>' + run(val(f), bold=True, size=21) +
                     (('<w:r><w:br/></w:r>' + run(note, size=15, color="6D6A63")) if note else ""))
    fig_notes, fig_flagged = figure_check(c, m, mk, nw, data)
    if tiles:
        heading("At a glance")
        rows = [tiles[i:i + 3] for i in range(0, len(tiles), 3)]
        rows[-1] += [""] * (3 - len(rows[-1]))
        table(rows, [3200, 3200, 3200])


    if data["highlights"]:
        heading("Key points")
        for h in data["highlights"]:
            bullet(run(plain(h), size=19))
        para(after=60)

    prods = [p for p in c.get("products") or [] if isinstance(p, dict) and p.get("name")]
    if prods or val(c.get("positioning")):
        heading("What they sell")
        if val(c.get("positioning")):
            para(run(val(c.get("positioning")), size=19), after=60)
        for pr in prods:
            bullet(run(pr["name"] + ": ", bold=True, size=19), run(pr.get("what") or "", size=19))
        for label, f in (("Business model", c.get("business_model")), ("Customers", c.get("customers")),
                         ("Investors", m.get("investors"))):
            if val(f):
                para(run(label + ": ", bold=True, size=18, color="6D6A63"), run(plain(val(f)), size=18), after=20)
        para(after=60)

    rivals = [r for r in mk.get("competitors") or [] if isinstance(r, dict) and r.get("name")]
    if rivals:
        heading("Main competitors")
        for r in rivals:
            name_run = link("https://" + host_of(r["website"]), r["name"], 19) if r.get("website") else \
                run(r["name"], bold=True, size=19)
            bullet(name_run, run(": " + (r.get("why") or ""), size=19))
        for label, f in [("Category", mk.get("category")), ("Position", mk.get("position"))] + edge_rows(mk):
            if val(f):
                para(run(label + ": ", bold=True, size=18, color="6D6A63"), run(plain(val(f)), size=18), after=20)
        para(after=60)

    if nw.get("items"):
        heading("Recent news")
        for i in nw["items"]:
            para(run(str(i.get("date") or "") + "    ", size=18, color="6D6A63"),
                 run(i.get("headline") or "", size=19), after=30)
        para(after=60)

    found = found_flags(nw, m, listed, run_date)
    if found:
        heading("Red flags")
        for f in found:
            bullet(run(f["label"] + ": ", bold=True, size=19, color="B3261E" if f["sev"] == "high" else "9A6A00"),
                   run(f["text"], size=19))
        para(after=60)

    nf = not_found_list(data, c, m, mk)
    if nf:
        para(run("Not found: ", bold=True, size=16, color="6D6A63"), run("; ".join(nf) + ".", size=16, color="6D6A63"))

    # Sources on their own page
    body.append('<w:p><w:r><w:br w:type="page"/></w:r></w:p>')
    heading("Sources")
    for src in data["sources"]:
        bullet(link(src["url"], src["title"], 18), run("  " + src["host"], size=15, color="6D6A63"))
    para(run("Built with Company Research for Claude Code from the public sources listed. "
             "Gathered by AI: check anything important at the source.", size=14, color="6D6A63"), after=0)

    W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" ' \
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    document = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document %s><w:body>%s'
                '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="850" w:right="900" w:bottom="850" '
                'w:left="900" w:header="400" w:footer="400" w:gutter="0"/></w:sectPr></w:body></w:document>'
                % (W, "".join(body)))
    styles = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:styles %s><w:docDefaults><w:rPrDefault>'
              '<w:rPr><w:rFonts w:ascii="Arial" w:hAnsi="Arial" w:cs="Arial"/><w:sz w:val="20"/>'
              '<w:color w:val="1B1B19"/></w:rPr></w:rPrDefault><w:pPrDefault><w:pPr><w:spacing w:after="80" '
              'w:line="264" w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults></w:styles>' % W)
    doc_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships '
                'xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
                'styles" Target="styles.xml"/>%s</Relationships>' % "".join(rels))
    ctypes = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types '
              'xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
              '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
              '<Default Extension="xml" ContentType="application/xml"/>'
              '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.'
              'wordprocessingml.document.main+xml"/><Override PartName="/word/styles.xml" ContentType="application/'
              'vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/></Types>')
    root_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships '
                 'xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" '
                 'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
                 'Target="word/document.xml"/></Relationships>')
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ctypes)
        z.writestr("_rels/.rels", root_rels)
        z.writestr("word/document.xml", document)
        z.writestr("word/styles.xml", styles)
        z.writestr("word/_rels/document.xml.rels", doc_rels)


def find_browser():
    """A Chromium-based browser for headless PDF printing (Chrome, Edge, Chromium, Brave)."""
    env = os.environ.get("BRIEF_RESEARCH_BROWSER")
    cands = [env] if env else []
    cands += [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
    ]
    for exe in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge",
                "msedge", "chrome"):
        found = shutil.which(exe)
        if found:
            cands.append(found)
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return None


def pdf_pages(path):
    data = Path(path).read_bytes()
    counts = [int(x) for x in re.findall(rb"/Type\s*/Pages\b[^>]*?/Count\s+(\d+)", data, re.S)]
    counts += [int(x) for x in re.findall(rb"/Count\s+(\d+)[^>]*?/Type\s*/Pages\b", data, re.S)]
    return max(counts) if counts else len(re.findall(rb"/Type\s*/Page\b(?!s)", data))


def make_pdf(browser, html_path, pdf_path, max_pages=2):
    """Print the report to PDF: the brief on page 1, sources from page 2.
    Shrink step by step until the brief fits on page 1 (so the whole PDF is max_pages)."""
    html_path, pdf_path = Path(html_path), Path(pdf_path)
    page = html_path.read_text(encoding="utf-8")
    tmp = html_path.with_name(".print.html")
    pages = 0
    for zoom in (1.0, 0.93, 0.87, 0.81, 0.75, 0.7, 0.65, 0.6):
        tmp.write_text(page.replace("<html lang=\"en\">", "<html lang=\"en\" style=\"zoom:%s\">" % zoom, 1),
                       encoding="utf-8")
        if pdf_path.exists():
            pdf_path.unlink()
        cmd = [browser, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
               "--hide-scrollbars", "--no-pdf-header-footer", "--print-to-pdf-no-header",
               "--virtual-time-budget=4000", "--print-to-pdf=" + str(pdf_path), tmp.resolve().as_uri()]
        try:
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=45)
        except Exception:
            break
        if not pdf_path.exists() or pdf_path.stat().st_size == 0:
            break
        pages = pdf_pages(pdf_path)
        if pages <= max_pages:
            break
    if tmp.exists():
        tmp.unlink()
    return pages if pdf_path.exists() and pdf_path.stat().st_size > 0 else 0


def write_atomic(path, text):
    """The live page refreshes itself, so never let it see a half-written file."""
    tmp = Path(path).with_name("." + Path(path).name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(str(tmp), str(path))


def build(run_dir, name, run_date, domain, open_browser=True):
    run_dir = Path(run_dir)
    today = parse_date(run_date) or date.today()
    domain = host_of(domain) if domain else ""
    data = merge(run_dir, domain, today)
    elapsed = 0
    started = run_dir / ".started"
    if started.exists():
        try:
            elapsed = int(time.time() - float(started.read_text().strip()))
        except ValueError:
            pass
    (run_dir / "data.json").write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    out = run_dir / "report.html"
    browser = find_browser()
    safe = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-") or "Company"
    stem = "%s-brief-%s" % (safe, today.isoformat())
    pdf_name, docx_name = stem + ".pdf", stem + ".docx"
    exports = {"html": stem + ".html"}
    try:
        export_docx(run_dir / docx_name, run_dir, name, today.isoformat(), domain, data)
        exports["docx"] = docx_name
    except Exception as exc:
        print("Word file could not be made (%s)." % exc)
    if browser:
        exports["pdf"] = pdf_name
    write_atomic(out, render(run_dir, name, today.isoformat(), domain, data, elapsed, exports))
    st = data["stats"]
    print("%s: %d facts from %d sources%s." % (name, st["verified"] + st["single_source"], st["sources"],
                                              (", ready in %ds" % elapsed) if elapsed else ""))
    for p in data["problems"]:
        print("WARNING " + p)
    print("Report: " + str(out.resolve()))
    if exports.get("docx"):
        print("Word: " + str((run_dir / exports["docx"]).resolve()))
    if open_browser:
        try:
            webbrowser.open(out.resolve().as_uri())
        except Exception:
            pass
    if browser:
        pages = make_pdf(browser, out, run_dir / pdf_name)
        if pages:
            print("PDF: %s (%d page%s)" % ((run_dir / pdf_name).resolve(), pages, "" if pages == 1 else "s"))
        else:  # no PDF after all: point the menu at the print dialog instead
            exports.pop("pdf", None)
            write_atomic(out, render(run_dir, name, today.isoformat(), domain, data, elapsed, exports))
            print("PDF could not be made; the report's button opens Save as PDF instead.")


def build_partial(run_dir, name, run_date, domain, pending):
    """Quick in-progress page: what has arrived so far, placeholders for the rest, refreshes itself."""
    run_dir = Path(run_dir)
    today = parse_date(run_date) or date.today()
    data = merge(run_dir, host_of(domain), today)
    page = render(run_dir, name, today.isoformat(), host_of(domain), data, 0, None, pending=tuple(pending))
    tmp = run_dir / ".report.tmp"
    tmp.write_text(page, encoding="utf-8")
    os.replace(str(tmp), str(run_dir / "report.html"))


def run(run_dir, name, run_date, domain, open_browser=True, wait=75):
    """One background command: logo, a live page that fills in as each agent lands, then the final build."""
    fetch_logo(run_dir, domain)
    run_dir = Path(run_dir)
    if (run_dir / ".done").exists():
        (run_dir / ".done").unlink()
    started = time.time()
    seen = None
    while time.time() - started < wait:
        ready = set()
        for n in FRAGMENTS:
            try:
                load_json_loose(run_dir / ("frag-%s.json" % n))
                ready.add(n)
            except Exception:
                pass
        if len(ready) == len(FRAGMENTS):
            break
        if ready != seen:
            build_partial(run_dir, name, run_date, domain, [n for n in FRAGMENTS if n not in ready])
            if seen is None:
                print("Live page open %.1fs after launch." % (time.time() - started + 0.0))
            if seen is None and open_browser:
                try:
                    webbrowser.open((run_dir / "report.html").resolve().as_uri())
                except Exception:
                    pass
            seen = ready
        time.sleep(0.3)
    import contextlib, io

    class Tee(io.StringIO):
        def write(self, s):
            sys.__stdout__.write(s)
            return super().write(s)
    buf = Tee()
    with contextlib.redirect_stdout(buf):
        build(run_dir, name, run_date, domain, open_browser=open_browser and seen is None)
    write_atomic(run_dir / ".done", buf.getvalue())


def wait_done(run_dir, timeout=90):
    """Block until the background run has written the final report, then print its summary."""
    run_dir = Path(run_dir)
    end = time.time() + timeout
    while time.time() < end:
        done = run_dir / ".done"
        if done.exists():
            print(done.read_text().strip())
            return
        time.sleep(0.3)
    print("Still building; the report will finish on its own in the browser.")


def main(a):
    if len(a) >= 4 and a[1] == "logo":
        fetch_logo(a[2], a[3])
        return 0
    if len(a) >= 3 and a[1] == "wait":
        wait_done(a[2])
        return 0
    if len(a) >= 6 and a[1] == "run":
        run(a[2], a[3], a[4], a[5], open_browser="--no-open" not in a)
        return 0
    if len(a) >= 6 and a[1] == "build":
        build(a[2], a[3], a[4], a[5], open_browser="--no-open" not in a)
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
