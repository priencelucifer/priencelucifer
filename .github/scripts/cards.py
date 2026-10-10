#!/usr/bin/env python3
"""Render the profile README cards as self-hosted SVGs.

Pulls live data from the GitHub GraphQL API and writes header, stats,
languages, streak and featured-repo cards into the output directory.
Standard library only, so the workflow needs no install step.

    GITHUB_TOKEN=... python3 cards.py OUT_DIR
"""
import datetime as dt
import html
import json
import math
import os
import sys
import textwrap
import time
import urllib.request

USER = os.environ.get("GH_USER", "priencelucifer")
TOKEN = os.environ["GITHUB_TOKEN"]
OUT = sys.argv[1] if len(sys.argv) > 1 else "dist"

# repo name -> (display name, fallback description when the repo has none)
FEATURED = {
    "esp32_corne_ble_keyboard": ("esp32_corne_ble_keyboard", "Wireless Corne split keyboard firmware running on the ESP32 over BLE."),
    "Low-cost-IoT-sensors-for-agriculture-and-environmental-monitoring.": ("iot-agri-sensors", None),
    "Pi-Pico-Web-Based-HID-Controller": ("pico-web-hid", None),
    "noteshub-public": ("noteshub", None),
}
LANGS_SHOWN = 6

# ---------------------------------------------------------------- theme
BG, PANEL, BORDER = "#0d1117", "#161b22", "#30363d"
GREEN, TEXT, MUTED, DIM = "#00ff66", "#c9d1d9", "#8b949e", "#484f58"
FONT = "'JetBrains Mono','Fira Code','SFMono-Regular',Consolas,'Liberation Mono',Menlo,monospace"

# GitHub's README column is 846px on desktop and ~308px on phones. 410px cards sit
# two per row on desktop and wrap to one per row on phones; the wide header/streak
# are only served to desktops (via <picture> in the README), phones get the compact ones.
CARD_W, WIDE_W = 410, 824

esc = html.escape


# ---------------------------------------------------------------- data
def gql(query, **variables):
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=body,
        headers={"Authorization": f"bearer {TOKEN}", "User-Agent": f"{USER}-readme-cards"},
    )
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = json.load(r)
            if "errors" in data:
                raise RuntimeError(data["errors"])
            return data["data"]
        except Exception as e:  # GitHub's GraphQL endpoint 502s now and then
            if attempt == 3:
                raise
            print(f"retrying after: {e}", file=sys.stderr)
            time.sleep(5 * (attempt + 1))


PROFILE_Q = """
query($login: String!, $cursor: String) {
  user(login: $login) {
    createdAt
    followers { totalCount }
    pullRequests { totalCount }
    issues { totalCount }
    repositoriesContributedTo(contributionTypes: [COMMIT, PULL_REQUEST, ISSUE, REPOSITORY]) { totalCount }
    contributionsCollection { totalCommitContributions contributionYears }
    repositories(first: 100, after: $cursor, ownerAffiliations: OWNER, isFork: false, privacy: PUBLIC) {
      pageInfo { hasNextPage endCursor }
      nodes {
        name description stargazerCount forkCount
        primaryLanguage { name color }
        languages(first: 10, orderBy: {field: SIZE, direction: DESC}) { edges { size node { name color } } }
      }
    }
  }
}"""


def fetch():
    repos, cursor = [], None
    while True:
        user = gql(PROFILE_Q, login=USER, cursor=cursor)["user"]
        page = user["repositories"]
        repos += page["nodes"]
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]

    now = dt.datetime.now(dt.timezone.utc)
    years = user["contributionsCollection"]["contributionYears"]
    parts = []
    for y in years:
        end = min(now, dt.datetime(y, 12, 31, 23, 59, 59, tzinfo=dt.timezone.utc))
        parts.append(
            f'y{y}: contributionsCollection(from: "{y}-01-01T00:00:00Z", to: "{end:%Y-%m-%dT%H:%M:%SZ}") '
            "{ contributionCalendar { weeks { contributionDays { date contributionCount } } } }"
        )
    cal = gql(f"query($login: String!) {{ user(login: $login) {{ {' '.join(parts)} }} }}", login=USER)["user"]
    days = {}
    for coll in cal.values():
        for week in coll["contributionCalendar"]["weeks"]:
            for d in week["contributionDays"]:
                days[d["date"]] = d["contributionCount"]
    today = now.date().isoformat()
    days = sorted((d, c) for d, c in days.items() if d <= today)
    return user, repos, days


def streaks(days):
    longest, run, start = (0, None, None), 0, None
    for d, c in days:
        if c:
            start = d if run == 0 else start
            run += 1
            if run > longest[0]:
                longest = (run, start, d)
        else:
            run = 0
    i = len(days) - 1
    if i >= 0 and days[i][1] == 0:  # today not over yet: don't break the streak
        i -= 1
    end, cur = i, 0
    while i >= 0 and days[i][1]:
        cur, i = cur + 1, i - 1
    current = (cur, days[i + 1][0] if cur else None, days[end][0] if cur else None)
    return current, longest


def languages(repos):
    # Same idea as github-readme-stats' size_weight=0.5&count_weight=0.5:
    # small firmware repos still register next to huge web projects.
    size, count, color = {}, {}, {}
    for r in repos:
        for e in r["languages"]["edges"]:
            n = e["node"]["name"]
            size[n] = size.get(n, 0) + e["size"]
            count[n] = count.get(n, 0) + 1
            color[n] = e["node"]["color"] or MUTED
    score = {n: math.sqrt(size[n]) * math.sqrt(count[n]) for n in size}
    total = sum(score.values()) or 1
    ranked = sorted(score, key=score.get, reverse=True)
    return [(n, score[n] / total * 100, color[n]) for n in ranked]


# ---------------------------------------------------------------- svg helpers
BASE_CSS = f"""
text {{ font-family: {FONT}; }}
.bar {{ fill: {MUTED}; font-size: 12px; }}
.in {{ opacity: 0; animation: fade .6s ease-out forwards; }}
@keyframes fade {{ from {{ opacity: 0; transform: translateY(6px); }} to {{ opacity: 1; transform: none; }} }}
@keyframes blink {{ 50% {{ opacity: 0; }} }}
@media (prefers-reduced-motion: reduce) {{ * {{ animation: none !important; }} .in {{ opacity: 1; }} }}
"""


def window(w, h, title, body, css="", defs=""):
    """Terminal-window chrome shared by every card."""
    return f"""<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="{w}" height="{h}" viewBox="0 0 {w} {h}" role="img" aria-label="{esc(title)}">
<title>{esc(title)}</title>
<style>{BASE_CSS}{css}</style>
<defs>
  <clipPath id="win"><rect width="{w}" height="{h}" rx="10"/></clipPath>
  <pattern id="scan" width="4" height="3" patternUnits="userSpaceOnUse"><rect width="4" height="1" fill="#fff" opacity=".022"/></pattern>
  <linearGradient id="edge" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0" stop-color="{GREEN}" stop-opacity=".6"/>
    <stop offset=".45" stop-color="{GREEN}" stop-opacity=".07"/>
    <stop offset="1" stop-color="{GREEN}" stop-opacity=".4"/>
  </linearGradient>
  <filter id="glow" x="-20%" y="-20%" width="140%" height="140%">
    <feGaussianBlur stdDeviation="2.4" result="b"/>
    <feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge>
  </filter>{defs}
</defs>
<g clip-path="url(#win)">
  <rect width="{w}" height="{h}" fill="{BG}"/>
  <rect width="{w}" height="30" fill="{PANEL}"/>
  <line x1="0" y1="30.5" x2="{w}" y2="30.5" stroke="{BORDER}"/>
  <circle cx="18" cy="15" r="5.5" fill="#ff5f57"/>
  <circle cx="36" cy="15" r="5.5" fill="#febc2e"/>
  <circle cx="54" cy="15" r="5.5" fill="#28c840"/>
  <text x="{w / 2}" y="19.5" text-anchor="middle" class="bar">{esc(title)}</text>
{body}
  <rect y="31" width="{w}" height="{h - 31}" fill="url(#scan)"/>
</g>
<rect x=".5" y=".5" width="{w - 1}" height="{h - 1}" rx="9.5" fill="none" stroke="url(#edge)"/>
</svg>
"""


def prompt(x, y, cmd, size=13):
    return (
        f'<text x="{x}" y="{y}" font-size="{size}"><tspan fill="{GREEN}" font-weight="700">ricky@github</tspan>'
        f'<tspan fill="{MUTED}">:</tspan><tspan fill="#58a6ff">~</tspan><tspan fill="{MUTED}">$ </tspan>'
        f'<tspan fill="{TEXT}">{esc(cmd)}</tspan></text>'
    )


def fmt_date(iso, year=True):
    d = dt.date.fromisoformat(iso)
    return f"{d:%b} {d.day}" + (f", {d.year}" if year else "")


def fmt_range(a, b):
    if not a:
        return "no active streak"
    if a == b:
        return fmt_date(a)
    same_year = a[:4] == b[:4]
    return f"{fmt_date(a, not same_year)} – {fmt_date(b)}"


# ---------------------------------------------------------------- header
RICKY = [
    "██████╗ ██╗ ██████╗██╗  ██╗██╗   ██╗",
    "██╔══██╗██║██╔════╝██║ ██╔╝╚██╗ ██╔╝",
    "██████╔╝██║██║     █████╔╝  ╚████╔╝ ",
    "██╔══██╗██║██║     ██╔═██╗   ╚██╔╝  ",
    "██║  ██║██║╚██████╗██║  ██╗   ██║   ",
    "╚═╝  ╚═╝╚═╝ ╚═════╝╚═╝  ╚═╝   ╚═╝   ",
]


def ansi_art(x0, y0, cw, ch, d=3):
    """Draw ANSI-Shadow text as geometry so it never depends on the viewer's fonts."""
    rects, path = [], []
    for r, row in enumerate(RICKY):
        y = y0 + r * ch
        c = 0
        while c < len(row):
            if row[c] == "█":  # merge runs of blocks into one rect: no hairline seams
                s = c
                while c < len(row) and row[c] == "█":
                    c += 1
                rects.append(f'<rect x="{x0 + s * cw}" y="{y}" width="{(c - s) * cw}" height="{ch + 0.5}"/>')
                continue
            x = x0 + c * cw
            L, R, T, B, cx, cy = x, x + cw, y, y + ch, x + cw / 2, y + ch / 2
            lines = {
                "═": [[(L, cy - d), (R, cy - d)], [(L, cy + d), (R, cy + d)]],
                "║": [[(cx - d, T), (cx - d, B)], [(cx + d, T), (cx + d, B)]],
                "╗": [[(L, cy - d), (cx + d, cy - d), (cx + d, B)], [(L, cy + d), (cx - d, cy + d), (cx - d, B)]],
                "╔": [[(R, cy - d), (cx - d, cy - d), (cx - d, B)], [(R, cy + d), (cx + d, cy + d), (cx + d, B)]],
                "╚": [[(cx - d, T), (cx - d, cy + d), (R, cy + d)], [(cx + d, T), (cx + d, cy - d), (R, cy - d)]],
                "╝": [[(cx + d, T), (cx + d, cy + d), (L, cy + d)], [(cx - d, T), (cx - d, cy - d), (L, cy - d)]],
            }.get(row[c], [])
            for pts in lines:
                path.append("M" + " L".join(f"{px:g} {py:g}" for px, py in pts))
            c += 1
    return "".join(rects), " ".join(path)


def header_card(compact=False):
    if compact:
        w, h, cw, ch, y0, tag_size = CARD_W, 268, 10, 19, 76, 14
        cmd, taglines = "figlet ricky", ["embedded · networking", "self-hosting · homelab"]
    else:
        w, h, cw, ch, y0, tag_size = WIDE_W, 300, 14, 26, 84, 13
        cmd, taglines = 'figlet -f "ANSI Shadow" ricky', ["embedded  ·  networking  ·  self-hosting  ·  homelab"]
    art_w = len(RICKY[0]) * cw
    x0 = (w - art_w) / 2
    blocks, shadow = ansi_art(x0, y0, cw, ch, d=cw * .22)
    css = f"""
.art {{ animation: flicker 7s infinite; }}
.ghost {{ opacity: 0; animation: glitch 7s infinite; }}
.ghost.b {{ animation-delay: .05s; }}
.beam {{ animation: sweep 6s linear infinite; }}
.cur {{ animation: blink 1s step-end infinite; }}
@keyframes flicker {{ 0%, 90%, 93%, 100% {{ opacity: 1; }} 91% {{ opacity: .55; }} 92% {{ opacity: .9; }} }}
@keyframes glitch {{
  0%, 89%, 96%, 100% {{ opacity: 0; transform: none; }}
  90% {{ opacity: .8; transform: translate(-5px, 1px); }}
  92% {{ opacity: .6; transform: translate(4px, -1px); }}
  94% {{ opacity: .8; transform: translate(-2px, 0); }}
}}
@keyframes sweep {{ from {{ transform: translateY(-60px); }} to {{ transform: translateY({h}px); }} }}
"""
    defs = f"""
  <pattern id="dots" width="18" height="18" patternUnits="userSpaceOnUse"><circle cx="9" cy="9" r=".9" fill="{GREEN}" opacity=".09"/></pattern>
  <linearGradient id="beam" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="{GREEN}" stop-opacity="0"/>
    <stop offset=".85" stop-color="{GREEN}" stop-opacity=".07"/>
    <stop offset="1" stop-color="{GREEN}" stop-opacity=".18"/>
  </linearGradient>
  <radialGradient id="vignette" cx=".5" cy=".55" r=".75">
    <stop offset=".55" stop-color="{BG}" stop-opacity="0"/>
    <stop offset="1" stop-color="#000" stop-opacity=".55"/>
  </radialGradient>
  <g id="blocks">{blocks}</g>"""
    ty = y0 + len(RICKY) * ch + 32
    tags = "".join(
        f'<text x="{w / 2}" y="{ty + i * 22}" text-anchor="middle" font-size="{tag_size}" fill="{MUTED}" letter-spacing=".5">{esc(t)}</text>'
        for i, t in enumerate(taglines)
    )
    last_y, last_w = ty + (len(taglines) - 1) * 22, len(taglines[-1]) * (tag_size * 0.6 + 0.5)
    body = f"""
  <rect y="31" width="{w}" height="{h}" fill="url(#dots)"/>
  <rect y="31" width="{w}" height="{h}" fill="url(#vignette)"/>
  {prompt(24 if compact else 28, 60 if compact else 62, cmd)}
  <g class="ghost"><use xlink:href="#blocks" fill="#ff2e88"/></g>
  <g class="ghost b"><use xlink:href="#blocks" fill="#00e5ff"/></g>
  <g class="art">
    <path d="{shadow}" fill="none" stroke="{GREEN}" stroke-opacity=".45" stroke-width="{cw * .115:.2f}" stroke-linejoin="miter"/>
    <use xlink:href="#blocks" fill="{GREEN}" filter="url(#glow)"/>
  </g>
  {tags}
  <rect class="cur" x="{w / 2 + last_w / 2 + 8:.1f}" y="{last_y - 11}" width="8" height="{tag_size + 1}" fill="{GREEN}"/>
  <rect class="beam" width="{w}" height="60" fill="url(#beam)"/>"""
    return window(w, h, "ricky@homelab: ~", body, css, defs)


# ---------------------------------------------------------------- stats (neofetch)
CHIP = [
    "....p.p.p.p....",
    "....p.p.p.p....",
    "..###########..",
    "..#o#########..",
    "pp###########pp",
    "..###ggggg###..",
    "pp###g***g###pp",
    "..###g***g###..",
    "pp###g***g###pp",
    "..###ggggg###..",
    "pp###########pp",
    "..###########..",
    "..###########..",
    "....p.p.p.p....",
    "....p.p.p.p....",
]


def chip(x0, y0, s):
    fills = {"#": "#123d26", "p": "#8b949e", "o": GREEN, "g": GREEN}
    out = []
    for r, row in enumerate(CHIP):
        for c, ch in enumerate(row):
            if ch == "*":
                out.append(f'<rect class="core" style="animation-delay:{(r * 3 + c) % 7 * .25:.2f}s" x="{x0 + c * s}" y="{y0 + r * s}" width="{s}" height="{s}" fill="{GREEN}"/>')
            elif ch in fills:
                out.append(f'<rect x="{x0 + c * s}" y="{y0 + r * s}" width="{s}" height="{s}" fill="{fills[ch]}"/>')
    return "\n  ".join(out)


def stats_card(user, repos, days, langs):
    w, h = CARD_W, 280
    created = dt.date.fromisoformat(user["createdAt"][:10])
    today = dt.date.today()
    months = (today.year - created.year) * 12 + today.month - created.month
    year = today.year
    rows = [
        ("User", USER),
        ("Uptime", f"{months // 12}y {months % 12}m (since {created.year})"),
        ("Repos", f"{len(repos)} public"),
        ("Stars", f"{sum(r['stargazerCount'] for r in repos):,}"),
        ("Commits", f"{user['contributionsCollection']['totalCommitContributions']:,} ({year})"),
        ("PRs", f"{user['pullRequests']['totalCount']:,}"),
        ("Issues", f"{user['issues']['totalCount']:,}"),
        ("Contribs", f"{sum(c for _, c in days):,} lifetime"),
        ("Followers", f"{user['followers']['totalCount']:,}"),
        ("Top lang", langs[0][0] if langs else "–"),
    ]
    kx, vx, y0, lh = 154, 242, 82, 17
    lines = [
        f'<text class="in" x="{kx}" y="{y0 - 22}" font-size="14" font-weight="700" fill="{GREEN}">ricky<tspan fill="{MUTED}">@</tspan>github</text>',
        f'<line class="in" x1="{kx}" y1="{y0 - 14}" x2="{kx + 150}" y2="{y0 - 14}" stroke="{DIM}" stroke-dasharray="4 3"/>',
    ]
    for i, (k, v) in enumerate(rows):
        y = y0 + i * lh
        lines.append(
            f'<g class="in" style="animation-delay:{.1 + i * .06:.2f}s">'
            f'<text x="{kx}" y="{y}" font-size="13" font-weight="700" fill="{GREEN}">{esc(k)}</text>'
            f'<text x="{vx}" y="{y}" font-size="13" fill="{TEXT}">{esc(v)}</text></g>'
        )
    palette = ["#ff5f57", "#febc2e", "#28c840", GREEN, "#2f81f7", "#a371f7", "#39c5cf", TEXT]
    by = y0 + len(rows) * lh + 2
    lines += [
        f'<rect class="in" style="animation-delay:.8s" x="{kx + i * 20}" y="{by}" width="18" height="10" rx="1" fill="{c}"/>'
        for i, c in enumerate(palette)
    ]
    css = f"""
.core {{ animation: pulse 1.75s ease-in-out infinite; }}
@keyframes pulse {{ 0%, 100% {{ opacity: .15; }} 50% {{ opacity: 1; }} }}
"""
    body = f"""
  <g filter="url(#glow)">
  {chip(20, 70, 8)}
  </g>
  <text x="80" y="214" text-anchor="middle" font-size="10" fill="{DIM}">RP2040 · ESP32</text>
  {''.join(lines)}"""
    return window(w, h, "neofetch", body, css)


# ---------------------------------------------------------------- languages
def langs_card(langs):
    w, h = CARD_W, 280
    top = langs[:LANGS_SHOWN]
    rest = sum(p for _, p, _ in langs[LANGS_SHOWN:])
    if rest > 0.05:
        top.append(("Other", rest, DIM))
    # stacked bar
    x, bar = 24, []
    span = w - 48
    for i, (n, p, c) in enumerate(top):
        seg = span * p / 100
        bar.append(f'<rect class="grow" style="animation-delay:{i * .08:.2f}s" x="{x:.2f}" y="66" width="{max(seg - 2, 1):.2f}" height="8" fill="{c}"/>')
        x += seg
    rows = []
    cells, cw = 20, 9
    for i, (n, p, c) in enumerate(top):
        y = 102 + i * 24
        lit = max(1, round(p / 100 * cells * 2)) if p else 0  # scale: 50% fills the meter
        lit = min(lit, cells)
        leds = "".join(
            f'<rect x="{150 + j * cw}" y="{y - 9}" width="{cw - 2}" height="10" rx="1" '
            f'fill="{c if j < lit else BORDER}" opacity="{1 if j < lit else .55}"/>'
            for j in range(cells)
        )
        rows.append(
            f'<g class="in" style="animation-delay:{.2 + i * .07:.2f}s">'
            f'<circle cx="30" cy="{y - 4}" r="4.5" fill="{c}"/>'
            f'<text x="44" y="{y}" font-size="13" fill="{TEXT}">{esc(n)}</text>'
            f"{leds}"
            f'<text x="{w - 24}" y="{y}" text-anchor="end" font-size="13" font-weight="700" fill="{GREEN}">{p:.1f}%</text></g>'
        )
    css = """
.grow { transform-box: fill-box; transform-origin: left; animation: grow .8s ease-out both; }
@keyframes grow { from { transform: scaleX(0); } to { transform: scaleX(1); } }
"""
    body = f"""
  {prompt(24, 54, "linguist --top --public")}
  <rect x="24" y="66" width="{span}" height="8" rx="2" fill="{BORDER}"/>
  {''.join(bar)}
  {''.join(rows)}"""
    return window(w, h, "languages", body, css)


# ---------------------------------------------------------------- streak
FLAME = "M0 -11 C5 -5 8 -1 8 4 C8 9 4 12 0 12 C-4 12 -8 9 -8 4 C-8 0 -5 -3 -3 -6 C-3 -2 -1 0 1 1 C2 -3 1 -7 0 -11 Z"


def streak_card(days, compact=False):
    (cur, cs, ce), (lng, ls, le) = streaks(days)
    total = sum(c for _, c in days)
    first = next((d for d, c in days if c), None)
    since = f"{fmt_date(first)} – Present" if first else ""
    r = 36
    circ = 2 * math.pi * r
    if compact:
        # ring on top, total / longest side by side underneath
        w, h = CARD_W, 368
        cx, cy = w / 2, 98
        sides = [(w / 4, total, "Total Contributions", since), (w * 3 / 4, lng, "Longest Streak", fmt_range(ls, le))]
        ny, nsize, ly, sy = 240, 28, 262, 281
        rules = [(24, 200, w - 24, 200), (w / 2, 214, w / 2, 286)]
        sx0, sx1, sy0, sy1 = 24, w - 24, 322, 352
    else:
        # total | ring | longest in three columns
        w, h = WIDE_W, 250
        cols = [w / 6, w / 2, w * 5 / 6]
        cx, cy = cols[1], 96
        sides = [(cols[0], total, "Total Contributions", since), (cols[2], lng, "Longest Streak", fmt_range(ls, le))]
        ny, nsize, ly, sy = 108, 32, 158, 177
        rules = [((cols[0] + cols[1]) / 2, 52, (cols[0] + cols[1]) / 2, 176), ((cols[1] + cols[2]) / 2, 52, (cols[1] + cols[2]) / 2, 176)]
        sx0, sx1, sy0, sy1 = 24, w - 24, 200, 236
    # 52-week activity sparkline
    tail = days[-364:]
    weeks = [sum(c for _, c in tail[i:i + 7]) for i in range(0, len(tail), 7)]
    peak = max(weeks, default=0) or 1
    pts = [(sx0 + (sx1 - sx0) * i / max(len(weeks) - 1, 1), sy1 - (sy1 - sy0) * v / peak) for i, v in enumerate(weeks)]
    line = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f} {y:.1f}" for i, (x, y) in enumerate(pts))
    area = f"{line} L{sx1} {sy1} L{sx0} {sy1} Z"
    css = f"""
.ring {{ stroke-dasharray: {circ:.1f}; stroke-dashoffset: {circ:.1f}; animation: draw 1.4s ease-out .2s forwards; }}
.trace {{ stroke-dasharray: 2400; stroke-dashoffset: 2400; animation: draw 2.4s ease-out .4s forwards; }}
.flame {{ transform-box: fill-box; transform-origin: center bottom; animation: flick 1.6s ease-in-out infinite; }}
@keyframes draw {{ to {{ stroke-dashoffset: 0; }} }}
@keyframes flick {{ 0%, 100% {{ transform: scale(1); }} 50% {{ transform: scale(1.08, .94); }} }}
"""
    defs = f"""
  <linearGradient id="area" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="{GREEN}" stop-opacity=".35"/>
    <stop offset="1" stop-color="{GREEN}" stop-opacity="0"/>
  </linearGradient>"""
    side = "".join(
        f'<g class="in" style="animation-delay:{.1 + i * .2:.1f}s">'
        f'<text x="{x:g}" y="{ny}" text-anchor="middle" font-size="{nsize}" font-weight="700" fill="{TEXT}">{n:,}</text>'
        f'<text x="{x:g}" y="{ly}" text-anchor="middle" font-size="13" font-weight="700" fill="{GREEN}">{label}</text>'
        f'<text x="{x:g}" y="{sy}" text-anchor="middle" font-size="11.5" fill="{MUTED}">{esc(sub)}</text></g>'
        for i, (x, n, label, sub) in enumerate(sides)
    )
    rule = "".join(f'<line x1="{a:g}" y1="{b}" x2="{c:g}" y2="{d}" stroke="{BORDER}"/>' for a, b, c, d in rules)
    body = f"""
  {rule}
  {side}
  <g class="in" style="animation-delay:.2s">
    <circle cx="{cx:g}" cy="{cy}" r="{r}" fill="none" stroke="{BORDER}" stroke-width="5"/>
    <circle class="ring" cx="{cx:g}" cy="{cy}" r="{r}" fill="none" stroke="{GREEN}" stroke-width="5" stroke-linecap="round" transform="rotate(-90 {cx:g} {cy})" filter="url(#glow)"/>
    <circle cx="{cx:g}" cy="{cy - r}" r="13" fill="{BG}"/>
    <g transform="translate({cx:g} {cy - r - 1})"><path class="flame" d="{FLAME}" fill="{GREEN}" filter="url(#glow)"/></g>
    <text x="{cx:g}" y="{cy + 11}" text-anchor="middle" font-size="30" font-weight="700" fill="{TEXT}">{cur:,}</text>
    <text x="{cx:g}" y="{cy + 62}" text-anchor="middle" font-size="13" font-weight="700" fill="{GREEN}">Current Streak</text>
    <text x="{cx:g}" y="{cy + 81}" text-anchor="middle" font-size="11.5" fill="{MUTED}">{esc(fmt_range(cs, ce))}</text>
  </g>
  <line x1="{sx0}" y1="{sy1 + .5}" x2="{sx1}" y2="{sy1 + .5}" stroke="{BORDER}"/>
  <text x="{sx0}" y="{sy0 - 6}" font-size="10" fill="{DIM}">activity · last 52 weeks</text>
  <text x="{sx1}" y="{sy0 - 6}" text-anchor="end" font-size="10" fill="{DIM}">peak {peak:,}/wk</text>
  <path class="in" style="animation-delay:.8s" d="{area}" fill="url(#area)"/>
  <path class="trace" d="{line}" fill="none" stroke="{GREEN}" stroke-width="1.6" stroke-linejoin="round" filter="url(#glow)"/>"""
    return window(w, h, "streak --watch", body, css, defs)


# ---------------------------------------------------------------- repo cards
def star(x, y, r=6):
    pts = []
    for i in range(10):
        a = -math.pi / 2 + i * math.pi / 5
        rr = r if i % 2 == 0 else r * 0.45
        pts.append(f"{x + rr * math.cos(a):.2f},{y + rr * math.sin(a):.2f}")
    return f'<polygon points="{" ".join(pts)}" fill="none" stroke="{MUTED}" stroke-width="1.3" stroke-linejoin="round"/>'


def fork(x, y):
    return (
        f'<g fill="none" stroke="{MUTED}" stroke-width="1.3" transform="translate({x} {y})">'
        f'<circle cx="-4" cy="-5" r="1.8"/><circle cx="4" cy="-5" r="1.8"/><circle cx="0" cy="6" r="1.8"/>'
        f'<path d="M-4 -3 V-1 Q-4 1 -2 1 H2 Q4 1 4 -1 V-3 M0 1 V4"/></g>'
    )


def repo_card(repo, display, fallback):
    w, h = CARD_W, 162
    desc = repo["description"] or fallback or "No description yet."
    lines = textwrap.wrap(desc, 48)
    if len(lines) > 3:
        lines = lines[:3]
        lines[2] = lines[2][:45].rstrip() + "…"
    lang = repo["primaryLanguage"] or {"name": "—", "color": MUTED}
    lang_w = len(lang["name"]) * 12.5 * 0.6
    sx = 44 + lang_w + 26
    texts = "".join(
        f'<text x="22" y="{82 + i * 18}" font-size="12.5" fill="{MUTED}">{esc(t)}</text>' for i, t in enumerate(lines)
    )
    body = f"""
  <g class="in">
  <text x="22" y="60" font-size="15" font-weight="700" fill="{GREEN}">{esc(display)}<tspan fill="{DIM}" font-weight="400"> /</tspan></text>
  {texts}
  <circle cx="28" cy="{h - 22}" r="5.5" fill="{lang['color'] or MUTED}"/>
  <text x="40" y="{h - 18}" font-size="12.5" fill="{TEXT}">{esc(lang['name'])}</text>
  {star(sx, h - 23)}
  <text x="{sx + 11}" y="{h - 18}" font-size="12.5" fill="{TEXT}">{repo['stargazerCount']:,}</text>
  {fork(sx + 46, h - 22)}
  <text x="{sx + 56}" y="{h - 18}" font-size="12.5" fill="{TEXT}">{repo['forkCount']:,}</text>
  </g>"""
    return window(w, h, f"~/{display}", body)


# ---------------------------------------------------------------- main
def main():
    user, repos, days = fetch()
    langs = languages(repos)
    by_name = {r["name"]: r for r in repos}
    os.makedirs(OUT, exist_ok=True)
    cards = {
        "header.svg": header_card(),
        "header-compact.svg": header_card(compact=True),
        "stats.svg": stats_card(user, repos, days, langs),
        "languages.svg": langs_card(langs),
        "streak.svg": streak_card(days),
        "streak-compact.svg": streak_card(days, compact=True),
    }
    for name, (display, fallback) in FEATURED.items():
        if name not in by_name:
            sys.exit(f"featured repo {name!r} not found among public repos")
        cards[f"repo-{display}.svg"] = repo_card(by_name[name], display, fallback)
    for fname, svg in cards.items():
        with open(os.path.join(OUT, fname), "w", encoding="utf-8") as f:
            f.write(svg)
        print(f"wrote {fname} ({len(svg):,} bytes)")


if __name__ == "__main__":
    main()
