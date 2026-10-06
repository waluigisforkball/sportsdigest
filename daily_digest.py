#!/usr/bin/env python3
"""
Daily Sports Digest
Fetches today's games across MLB, NFL, NBA, NHL, and EPL from ESPN's public
scoreboard API, formats them in Mike's local timezone, and posts a Discord
embed via webhook.

Runs on a GitHub Actions cron. Because GitHub Actions cron is fixed in UTC
and Eastern Time shifts with DST, the workflow schedules TWO triggers
(covering both EST and EDT offsets). Scheduled runs can also fire late
(GitHub does not guarantee exact timing), so instead of requiring an exact
hour match, this script uses a generous window PLUS a dedupe file
(last_post_date.txt, committed back to the repo) so whichever trigger fires
first each day posts, and the second one is skipped as a duplicate rather
than missed by a strict time check.
"""

import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

LOCAL_TZ = ZoneInfo("America/New_York")
WINDOW_START_HOUR = 5   # generous morning window to absorb GitHub scheduling
WINDOW_END_HOUR = 11    # delays; actual double-post prevention is the dedupe file below
LAST_POST_FILE = "last_post_date.txt"

WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

# Sport display order = priority order. (name, ESPN sport path, ESPN league path)
LEAGUES = [
    ("MLB", "baseball", "mlb"),
    ("NFL", "football", "nfl"),
    ("NBA", "basketball", "nba"),
    ("NHL", "hockey", "nhl"),
    ("EPL", "soccer", "eng.1"),
]

SPORT_EMOJI = {
    "MLB": "⚾",
    "NFL": "🏈",
    "NBA": "🏀",
    "NHL": "🏒",
    "EPL": "⚽",
    "Tennis": "🎾",
    "Golf": "⛳",
}

# Favorite teams get bolded in the matchup line (Discord markdown).
FAVORITE_TEAMS = {"Orioles", "Ravens", "Jazz", "Mammoth"}

# Only surface tennis/golf when one of these is actually happening —
# case-insensitive substring match against the tournament name ESPN returns.
MAJOR_TENNIS_KEYWORDS = ["australian open", "french open", "roland garros", "wimbledon", "us open"]
MAJOR_GOLF_KEYWORDS = ["masters tournament", "pga championship", "u.s. open", "open championship"]

# Discord embed color (hex int) - Memphis-style accent
EMBED_COLOR = 0x1A6EF5


def fetch_league_scoreboard(sport_path: str, league_path: str, date_str: str):
    """Fetch today's full scoreboard payload for a given ESPN sport/league."""
    url = f"https://site.api.espn.com/apis/site/v2/sports/{sport_path}/{league_path}/scoreboard"
    params = {"dates": date_str}
    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        print(f"[warn] failed to fetch {league_path}: {e}", file=sys.stderr)
        return {}


def extract_league_logo(payload: dict) -> str | None:
    """Pull the league logo URL out of a scoreboard payload, if present."""
    leagues = payload.get("leagues", [])
    if leagues:
        logos = leagues[0].get("logos", [])
        if logos:
            return logos[0].get("href")
    return None


def format_game_line(event: dict) -> str:
    """Build a single line: matchup, local time, channel (if known)."""
    competitions = event.get("competitions", [{}])
    comp = competitions[0] if competitions else {}

    competitors = comp.get("competitors", [])
    home = next((c for c in competitors if c.get("homeAway") == "home"), None)
    away = next((c for c in competitors if c.get("homeAway") == "away"), None)

    def team_name(c):
        if not c:
            return "TBD"
        name = c.get("team", {}).get("shortDisplayName") or c.get("team", {}).get("displayName", "TBD")
        if name in FAVORITE_TEAMS:
            return f"**{name}**"
        return name

    matchup = f"{team_name(away)} @ {team_name(home)}"

    # Time
    date_iso = event.get("date")  # UTC ISO string
    time_str = "TBD"
    if date_iso:
        try:
            utc_dt = datetime.fromisoformat(date_iso.replace("Z", "+00:00"))
            local_dt = utc_dt.astimezone(LOCAL_TZ)
            time_str = local_dt.strftime("%-I:%M %p ET")
        except ValueError:
            pass

    # Status (e.g. postponed, final already, etc.) - only note if not scheduled
    status_type = comp.get("status", {}).get("type", {}).get("state")
    status_note = ""
    if status_type == "post":
        status_note = " (final)"
    elif status_type == "in":
        status_note = " (in progress)"

    # Broadcast / channel info
    channel = None
    broadcasts = comp.get("broadcasts", [])
    if broadcasts:
        names = broadcasts[0].get("names", [])
        if names:
            channel = "/".join(names)
    if not channel:
        geo_broadcasts = comp.get("geoBroadcasts", [])
        if geo_broadcasts:
            media = geo_broadcasts[0].get("media", {})
            channel = media.get("shortName")

    line = f"{matchup} — {time_str}{status_note}"
    if channel:
        line += f" ({channel})"
    return line


def _tournament_name(payload: dict) -> str:
    """Best-effort extraction of the current tournament/event name."""
    leagues = payload.get("leagues", [])
    if leagues and leagues[0].get("name"):
        return leagues[0]["name"]
    events = payload.get("events", [])
    if events and events[0].get("name"):
        return events[0]["name"]
    return ""


def fetch_tennis_majors(date_str: str):
    """Check ATP + WTA scoreboards; only return matches if a Grand Slam is live today."""
    lines = []
    logo_url = None
    for tour in ("atp", "wta"):
        payload = fetch_league_scoreboard("tennis", tour, date_str)
        name = _tournament_name(payload).lower()
        if not any(kw in name for kw in MAJOR_TENNIS_KEYWORDS):
            continue  # not a major right now (or no event at all) — skip this tour
        if logo_url is None:
            logo_url = extract_league_logo(payload)
        for event in payload.get("events", []):
            comp = (event.get("competitions") or [{}])[0]
            competitors = comp.get("competitors", [])

            def player_name(c):
                if not c:
                    return "TBD"
                athlete = c.get("athlete", {})
                return athlete.get("shortName") or athlete.get("displayName", "TBD")

            p1 = next((c for c in competitors if c.get("order") == 1), competitors[0] if competitors else None)
            p2 = next((c for c in competitors if c.get("order") == 2),
                       competitors[1] if len(competitors) > 1 else None)
            matchup = f"{player_name(p1)} vs {player_name(p2)}"

            date_iso = event.get("date")
            time_str = "TBD"
            if date_iso:
                try:
                    utc_dt = datetime.fromisoformat(date_iso.replace("Z", "+00:00"))
                    time_str = utc_dt.astimezone(LOCAL_TZ).strftime("%-I:%M %p ET")
                except ValueError:
                    pass

            round_name = event.get("shortName") or comp.get("notes", [{}])[0].get("headline", "")
            line = f"{matchup} — {time_str}"
            if round_name:
                line += f" ({round_name})"
            lines.append(line)
    return lines, logo_url


def fetch_golf_majors(date_str: str):
    """Check the PGA scoreboard; only return a summary if a major is underway today."""
    payload = fetch_league_scoreboard("golf", "pga", date_str)
    name = _tournament_name(payload).lower()
    if not any(kw in name for kw in MAJOR_GOLF_KEYWORDS):
        return [], None

    logo_url = extract_league_logo(payload)
    lines = []
    for event in payload.get("events", []):
        tourney_name = event.get("name", "Golf Major")
        status_detail = event.get("status", {}).get("type", {}).get("detail", "")
        line = f"{tourney_name}"
        if status_detail:
            line += f" — {status_detail}"
        lines.append(line)
    return lines, logo_url


def build_embeds(league_data: dict, today_label: str) -> list[dict]:
    """One embed per league (with its logo as thumbnail), plus a title embed."""
    embeds = [{
        "title": f"🗓️ Sports Digest — {today_label}",
        "color": EMBED_COLOR,
    }]

    any_games = False
    for league_name, (lines, logo_url) in league_data.items():
        if not lines:
            continue  # omit sports with nothing on today
        any_games = True
        emoji = SPORT_EMOJI.get(league_name, "")
        embed = {
            "title": f"{emoji} {league_name}",
            "description": "\n".join(lines),
            "color": EMBED_COLOR,
        }
        if logo_url:
            embed["thumbnail"] = {"url": logo_url}
        embeds.append(embed)

    if not any_games:
        embeds.append({
            "title": "Nothing today",
            "description": "No games found across your tracked leagues.",
            "color": EMBED_COLOR,
        })

    embeds[-1]["footer"] = {"text": "Times shown in Eastern (ET)"}
    return embeds


def already_posted_today(today_str: str) -> bool:
    if not os.path.exists(LAST_POST_FILE):
        return False
    try:
        with open(LAST_POST_FILE) as f:
            return f.read().strip() == today_str
    except OSError:
        return False


def mark_posted(today_str: str) -> None:
    with open(LAST_POST_FILE, "w") as f:
        f.write(today_str)


def main():
    now_local = datetime.now(LOCAL_TZ)
    force_run = os.environ.get("FORCE_RUN") == "true"
    today_str = now_local.strftime("%Y-%m-%d")

    # Generous window: absorbs GitHub Actions scheduling delays. Manual
    # "Run workflow" triggers set FORCE_RUN=true to skip this entirely.
    if not force_run and not (WINDOW_START_HOUR <= now_local.hour <= WINDOW_END_HOUR):
        print(f"[skip] local hour is {now_local.hour}, outside window "
              f"{WINDOW_START_HOUR}-{WINDOW_END_HOUR}. Exiting.")
        return

    # Dedupe: if today's digest already went out (from the other DST trigger
    # firing earlier), don't post again.
    if not force_run and already_posted_today(today_str):
        print(f"[skip] already posted today ({today_str}). Exiting.")
        return

    if not WEBHOOK_URL:
        print("[error] DISCORD_WEBHOOK_URL is not set", file=sys.stderr)
        sys.exit(1)

    date_str = now_local.strftime("%Y%m%d")
    today_label = now_local.strftime("%A, %B %-d")

    league_data = {}
    for league_name, sport_path, league_path in LEAGUES:
        payload = fetch_league_scoreboard(sport_path, league_path, date_str)
        events = payload.get("events", [])
        lines = [format_game_line(e) for e in events]
        logo_url = extract_league_logo(payload)
        league_data[league_name] = (lines, logo_url)

    # Tennis/golf: only show up when a major is actually underway
    league_data["Tennis"] = fetch_tennis_majors(date_str)
    league_data["Golf"] = fetch_golf_majors(date_str)

    embeds = build_embeds(league_data, today_label)

    # Discord allows max 10 embeds per message
    resp = requests.post(WEBHOOK_URL, json={"embeds": embeds[:10]}, timeout=15)
    if resp.status_code >= 300:
        print(f"[error] Discord webhook failed: {resp.status_code} {resp.text}", file=sys.stderr)
        sys.exit(1)

    if not force_run:
        mark_posted(today_str)

    print("[ok] Digest posted.")


if __name__ == "__main__":
    main()
