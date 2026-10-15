#!/usr/bin/env python3
"""
Daily Sports Digest
Fetches today's games from ESPN's public scoreboard API (MLB, NFL, NBA, NHL,
EPL, Champions League, rugby, tennis/golf majors) and MLB's Stats API
(international baseball: NPB, KBO, ABL, Mexican leagues), formats them in
Mike's local timezone, and posts Discord embeds via webhook.

Triggering: cron-job.org fires workflow_dispatch each morning (GitHub's own
schedule trigger is unreliable). The native cron entries stay as a backup;
a generous time window plus a same-day dedupe file (last_post_date.txt)
stop them from double-posting.

Probe mode (PROBE=true, set via the workflow's "probe" input) prints what
each endpoint returns to the Actions log and posts nothing. Use it to verify
league IDs / sport IDs for the less common leagues.
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

LOCAL_TZ = ZoneInfo("America/New_York")
WINDOW_START_HOUR = 5   # generous morning window to absorb scheduling delays;
WINDOW_END_HOUR = 11    # actual double-post prevention is the dedupe file below
LAST_POST_FILE = "last_post_date.txt"

WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
PROBE = os.environ.get("PROBE") == "true"

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"
STATSAPI_BASE = "https://statsapi.mlb.com/api/v1"

EMBED_COLOR = 0x1A6EF5
MAX_DESC = 3800          # Discord embed description cap is 4096
MAX_EMBEDS_PER_MSG = 10  # Discord cap
MAX_CHARS_PER_MSG = 5500  # Discord caps total embed text at 6000 per message
FAR_FUTURE = datetime.max.replace(tzinfo=timezone.utc)

# Favorites: starred + bolded, and sorted to the top of their section.
FAVORITE_TEAMS = {"Orioles", "Ravens", "Jazz", "Mammoth"}

# Only surface tennis/golf when a major is actually underway
# (case-insensitive substring match on the tournament name ESPN returns).
MAJOR_TENNIS_KEYWORDS = ["australian open", "french open", "roland garros", "wimbledon", "us open"]
MAJOR_GOLF_KEYWORDS = ["masters tournament", "pga championship", "u.s. open", "open championship"]

# International baseball via MLB's Stats API. The script looks up /sports at
# runtime and matches these keywords against each sport's name. If a league
# isn't found (or matches the wrong thing), run probe mode, then pin its ID
# in "sport_id" below.
INTL_BASEBALL = {
    "NPB": {"keywords": ["nippon", "npb"], "sport_id": None},
    "KBO": {"keywords": ["korea", "kbo"], "sport_id": None},
    "ABL": {"keywords": ["australia"], "sport_id": None},
    "Mexico": {"keywords": ["mexic"], "sport_id": None},
}

# Display order = priority order.
# (section name, emoji, kind, config)
#   espn: config = [(sub-label or None, ESPN sport path, ESPN league path), ...]
#
# NOTE: the rugby league IDs below are from memory and UNVERIFIED. Run probe
# mode to confirm each returns the expected competition name.
SECTIONS = [
    ("MLB", "⚾", "espn", [(None, "baseball", "mlb")]),
    ("International Baseball", "🌏", "intl_baseball", None),
    ("NFL", "🏈", "espn", [(None, "football", "nfl")]),
    ("NBA", "🏀", "espn", [(None, "basketball", "nba")]),
    ("NHL", "🏒", "espn", [(None, "hockey", "nhl")]),
    ("EPL", "⚽", "espn", [(None, "soccer", "eng.1")]),
    ("Champions League", "🏆", "espn", [(None, "soccer", "uefa.champions")]),
    ("Rugby", "🏉", "espn", [
        ("Six Nations", "rugby", "180659"),
        ("Rugby Championship", "rugby", "244293"),
    ]),
    ("Tennis", "🎾", "tennis", None),
    ("Golf", "⛳", "golf", None),
]


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def fmt_time(dt):
    if not dt:
        return "TBD"
    return dt.astimezone(LOCAL_TZ).strftime("%-I:%M %p ET")


def parse_iso(date_iso):
    if not date_iso:
        return None
    try:
        return datetime.fromisoformat(date_iso.replace("Z", "+00:00"))
    except ValueError:
        return None


def get_json(url, params=None):
    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except (requests.RequestException, ValueError) as e:
        print(f"[warn] failed to fetch {url}: {e}", file=sys.stderr)
        return {}


def fetch_espn_scoreboard(sport_path, league_path, date_str):
    return get_json(f"{ESPN_BASE}/{sport_path}/{league_path}/scoreboard", {"dates": date_str})


def extract_league_logo(payload):
    leagues = payload.get("leagues", [])
    if leagues:
        logos = leagues[0].get("logos", [])
        if logos:
            return logos[0].get("href")
    return None


def tournament_name(payload):
    leagues = payload.get("leagues", [])
    if leagues and leagues[0].get("name"):
        return leagues[0]["name"]
    events = payload.get("events", [])
    if events and events[0].get("name"):
        return events[0]["name"]
    return ""


# --------------------------------------------------------------------------
# Game formatting. Every fetcher returns (games, logo_url) where each game is
# {"line": str, "fav": bool, "dt": datetime | None}.
# --------------------------------------------------------------------------

def format_espn_game(event, label=None):
    comp = (event.get("competitions") or [{}])[0]
    competitors = comp.get("competitors", [])
    home = next((c for c in competitors if c.get("homeAway") == "home"), None)
    away = next((c for c in competitors if c.get("homeAway") == "away"), None)
    fav = False

    def team_name(c):
        nonlocal fav
        if not c:
            return "TBD"
        team = c.get("team", {})
        name = team.get("shortDisplayName") or team.get("displayName", "TBD")
        if name in FAVORITE_TEAMS:
            fav = True
            return f"⭐ **{name}**"
        return name

    matchup = f"{team_name(away)} @ {team_name(home)}"
    dt = parse_iso(event.get("date"))

    state = comp.get("status", {}).get("type", {}).get("state")
    note = " (final)" if state == "post" else " (in progress)" if state == "in" else ""

    channel = None
    broadcasts = comp.get("broadcasts", [])
    if broadcasts:
        names = broadcasts[0].get("names", [])
        if names:
            channel = "/".join(names)
    if not channel:
        geo = comp.get("geoBroadcasts", [])
        if geo:
            channel = geo[0].get("media", {}).get("shortName")

    line = f"{matchup} — {fmt_time(dt)}{note}"
    if channel:
        line += f" ({channel})"
    if label:
        line = f"{label}: {line}"
    return {"line": line, "fav": fav, "dt": dt}


def fetch_espn_section(sources, date_str):
    games, logo = [], None
    for label, sport_path, league_path in sources:
        payload = fetch_espn_scoreboard(sport_path, league_path, date_str)
        if logo is None:
            logo = extract_league_logo(payload)
        for event in payload.get("events", []):
            games.append(format_espn_game(event, label))
    return games, logo


def fetch_tennis_majors(date_str):
    """ATP + WTA; only returns matches if a Grand Slam is live today."""
    games, logo = [], None
    for tour in ("atp", "wta"):
        payload = fetch_espn_scoreboard("tennis", tour, date_str)
        name = tournament_name(payload).lower()
        if not any(kw in name for kw in MAJOR_TENNIS_KEYWORDS):
            continue
        if logo is None:
            logo = extract_league_logo(payload)
        for event in payload.get("events", []):
            comp = (event.get("competitions") or [{}])[0]
            competitors = comp.get("competitors", [])

            def player_name(c):
                if not c:
                    return "TBD"
                athlete = c.get("athlete", {})
                return athlete.get("shortName") or athlete.get("displayName", "TBD")

            p1 = next((c for c in competitors if c.get("order") == 1),
                      competitors[0] if competitors else None)
            p2 = next((c for c in competitors if c.get("order") == 2),
                      competitors[1] if len(competitors) > 1 else None)
            dt = parse_iso(event.get("date"))
            notes = comp.get("notes") or [{}]
            round_name = event.get("shortName") or notes[0].get("headline", "")
            line = f"{player_name(p1)} vs {player_name(p2)} — {fmt_time(dt)}"
            if round_name:
                line += f" ({round_name})"
            games.append({"line": line, "fav": False, "dt": dt})
    return games, logo


def fetch_golf_majors(date_str):
    """PGA scoreboard; only returns a summary if a major is underway today."""
    payload = fetch_espn_scoreboard("golf", "pga", date_str)
    name = tournament_name(payload).lower()
    if not any(kw in name for kw in MAJOR_GOLF_KEYWORDS):
        return [], None
    games = []
    for event in payload.get("events", []):
        line = event.get("name", "Golf Major")
        detail = event.get("status", {}).get("type", {}).get("detail", "")
        if detail:
            line += f" — {detail}"
        games.append({"line": line, "fav": False, "dt": parse_iso(event.get("date"))})
    return games, extract_league_logo(payload)


def statsapi_sports():
    return get_json(f"{STATSAPI_BASE}/sports").get("sports", [])


def resolve_intl_sport_ids(sports):
    """Map league label -> list of (sport_id) using pinned IDs or name keywords."""
    resolved = {}
    for label, cfg in INTL_BASEBALL.items():
        if cfg["sport_id"]:
            resolved[label] = [cfg["sport_id"]]
            continue
        ids = []
        for s in sports:
            haystack = f"{s.get('name', '')} {s.get('abbreviation', '')}".lower()
            if any(kw in haystack for kw in cfg["keywords"]):
                ids.append(s["id"])
        resolved[label] = ids
    return resolved


def fetch_statsapi_games(sport_id, date_str_dashed):
    data = get_json(f"{STATSAPI_BASE}/schedule",
                    {"sportId": sport_id, "date": date_str_dashed, "hydrate": "team"})
    games = []
    for d in data.get("dates", []):
        games.extend(d.get("games", []))
    return games


def fetch_intl_baseball(date_dashed):
    sports = statsapi_sports()
    resolved = resolve_intl_sport_ids(sports)
    out = []
    for label, ids in resolved.items():
        if not ids:
            print(f"[info] no Stats API sport matched for {label}; skipping", file=sys.stderr)
        for sid in ids:
            for g in fetch_statsapi_games(sid, date_dashed):
                teams = g.get("teams", {})
                away = teams.get("away", {}).get("team", {}).get("name", "TBD")
                home = teams.get("home", {}).get("team", {}).get("name", "TBD")
                dt = parse_iso(g.get("gameDate"))
                status = g.get("status", {})
                detailed = status.get("detailedState", "")
                state = status.get("abstractGameState", "")
                note = ""
                if "postpone" in detailed.lower() or "cancel" in detailed.lower():
                    note = f" ({detailed.lower()})"
                elif state == "Final":
                    note = " (final)"
                elif state == "Live":
                    note = " (in progress)"
                out.append({"line": f"{label}: {away} @ {home} — {fmt_time(dt)}{note}",
                            "fav": False, "dt": dt})
    return out, None


# --------------------------------------------------------------------------
# Collect + build
# --------------------------------------------------------------------------

def collect_sections(date_str, date_dashed):
    results = []
    for name, emoji, kind, cfg in SECTIONS:
        try:
            if kind == "espn":
                games, logo = fetch_espn_section(cfg, date_str)
            elif kind == "intl_baseball":
                games, logo = fetch_intl_baseball(date_dashed)
            elif kind == "tennis":
                games, logo = fetch_tennis_majors(date_str)
            elif kind == "golf":
                games, logo = fetch_golf_majors(date_str)
            else:
                games, logo = [], None
        except Exception as e:  # one broken league must never kill the digest
            print(f"[warn] section {name} failed: {e!r}", file=sys.stderr)
            games, logo = [], None
        games.sort(key=lambda g: (not g["fav"], g["dt"] or FAR_FUTURE))
        results.append((name, emoji, games, logo))
    return results


def section_embeds(name, emoji, games, logo):
    chunks, cur, size = [], [], 0
    for g in games:
        line = g["line"]
        if cur and size + len(line) + 1 > MAX_DESC:
            chunks.append(cur)
            cur, size = [], 0
        cur.append(line)
        size += len(line) + 1
    if cur:
        chunks.append(cur)

    embeds = []
    for i, chunk in enumerate(chunks):
        embed = {
            "title": f"{emoji} {name}" + (" (cont.)" if i else ""),
            "description": "\n".join(chunk),
            "color": EMBED_COLOR,
        }
        if logo and i == 0:
            embed["thumbnail"] = {"url": logo}
        embeds.append(embed)
    return embeds


def embed_chars(e):
    return (len(e.get("title", "")) + len(e.get("description", ""))
            + len(e.get("footer", {}).get("text", "")))


def pack_messages(embeds):
    """Greedy-pack embeds into messages under Discord's count and char caps."""
    msgs, cur, chars = [], [], 0
    for e in embeds:
        c = embed_chars(e)
        if cur and (len(cur) >= MAX_EMBEDS_PER_MSG or chars + c > MAX_CHARS_PER_MSG):
            msgs.append(cur)
            cur, chars = [], 0
        cur.append(e)
        chars += c
    if cur:
        msgs.append(cur)
    return msgs


def build_messages(sections, today_label):
    embeds = [{"title": f"🗓️ Sports Digest — {today_label}", "color": EMBED_COLOR}]
    any_games = False
    for name, emoji, games, logo in sections:
        if not games:
            continue  # omit sports with nothing on today
        any_games = True
        embeds.extend(section_embeds(name, emoji, games, logo))
    if not any_games:
        embeds.append({"title": "Nothing today",
                       "description": "No games found across your tracked leagues.",
                       "color": EMBED_COLOR})
    msgs = pack_messages(embeds)
    msgs[-1][-1]["footer"] = {"text": "Times shown in Eastern (ET) · ⭐ = your teams"}
    return msgs


def post_message(embeds):
    for attempt in range(2):
        resp = requests.post(WEBHOOK_URL, json={"embeds": embeds}, timeout=15)
        if resp.status_code == 429 and attempt == 0:
            try:
                wait = float(resp.json().get("retry_after", 2))
            except (ValueError, AttributeError):
                wait = 2
            time.sleep(min(wait, 10))
            continue
        return resp
    return resp


# --------------------------------------------------------------------------
# Probe mode
# --------------------------------------------------------------------------

def run_probe(date_str, date_dashed):
    print(f"=== PROBE for {date_dashed} (nothing will be posted) ===\n")

    print("--- ESPN sections ---")
    for name, _, kind, cfg in SECTIONS:
        if kind != "espn":
            continue
        for label, sport_path, league_path in cfg:
            payload = fetch_espn_scoreboard(sport_path, league_path, date_str)
            leagues = payload.get("leagues", [])
            lname = leagues[0].get("name") if leagues else None
            n = len(payload.get("events", []))
            print(f"{name}{' / ' + label if label else ''} [{sport_path}/{league_path}]: "
                  f"competition={lname!r} events_today={n}")

    print("\n--- Tennis / golf current tournament ---")
    for tour in ("atp", "wta"):
        p = fetch_espn_scoreboard("tennis", tour, date_str)
        print(f"tennis/{tour}: {tournament_name(p)!r} events={len(p.get('events', []))}")
    p = fetch_espn_scoreboard("golf", "pga", date_str)
    print(f"golf/pga: {tournament_name(p)!r} events={len(p.get('events', []))}")

    print("\n--- ESPN rugby competition list (to find league IDs) ---")
    data = get_json("https://sports.core.api.espn.com/v2/sports/rugby/leagues", {"limit": 200})
    print(json.dumps(data, indent=1)[:3000] or "(empty)")

    print("\n--- MLB Stats API sports (id | name | games today) ---")
    sports = statsapi_sports()
    for s in sports:
        n = len(fetch_statsapi_games(s["id"], date_dashed))
        print(f"{s['id']} | {s.get('name')} | {s.get('abbreviation')} | games_today={n}")

    print("\n--- Resolved international baseball matches ---")
    for label, ids in resolve_intl_sport_ids(sports).items():
        print(f"{label}: sport ids {ids or 'NONE FOUND'}")


# --------------------------------------------------------------------------
# Dedupe + main
# --------------------------------------------------------------------------

def already_posted_today(today_str):
    if not os.path.exists(LAST_POST_FILE):
        return False
    try:
        with open(LAST_POST_FILE) as f:
            return f.read().strip() == today_str
    except OSError:
        return False


def mark_posted(today_str):
    with open(LAST_POST_FILE, "w") as f:
        f.write(today_str)


def main():
    now_local = datetime.now(LOCAL_TZ)
    force_run = os.environ.get("FORCE_RUN") == "true"
    today_str = now_local.strftime("%Y-%m-%d")
    date_str = now_local.strftime("%Y%m%d")

    if PROBE:
        run_probe(date_str, today_str)
        return

    # Window gate: manual/external dispatches set FORCE_RUN=true to skip it.
    if not force_run and not (WINDOW_START_HOUR <= now_local.hour <= WINDOW_END_HOUR):
        print(f"[skip] local hour {now_local.hour} outside window "
              f"{WINDOW_START_HOUR}-{WINDOW_END_HOUR}. Exiting.")
        return

    if not force_run and already_posted_today(today_str):
        print(f"[skip] already posted today ({today_str}). Exiting.")
        return

    if not WEBHOOK_URL:
        print("[error] DISCORD_WEBHOOK_URL is not set", file=sys.stderr)
        sys.exit(1)

    today_label = now_local.strftime("%A, %B %-d")
    sections = collect_sections(date_str, today_str)
    messages = build_messages(sections, today_label)

    failures, posted_any = 0, False
    for i, embeds in enumerate(messages):
        if i:
            time.sleep(1)
        resp = post_message(embeds)
        if resp.status_code >= 300:
            failures += 1
            print(f"[error] Discord message {i + 1}/{len(messages)} failed: "
                  f"{resp.status_code} {resp.text}", file=sys.stderr)
        else:
            posted_any = True

    # Mark as posted if anything went out: a partial digest beats a duplicate.
    if posted_any and not force_run:
        mark_posted(today_str)

    if failures:
        sys.exit(1)
    print(f"[ok] Digest posted ({len(messages)} message(s)).")


if __name__ == "__main__":
    main()
