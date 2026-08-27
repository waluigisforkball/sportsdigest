#!/usr/bin/env python3
"""
Daily Sports Digest
Fetches today's games across MLB, NFL, NBA, NHL, and EPL from ESPN's public
scoreboard API, formats them in Mike's local timezone, and posts a Discord
embed via webhook.

Runs on a GitHub Actions cron. Because GitHub Actions cron is fixed in UTC
and Eastern Time shifts with DST, the workflow schedules TWO triggers
(covering both EST and EDT offsets) and this script checks the current
Eastern hour and exits early unless it's in the target window. This means
it fires once per day at ~6-7am ET regardless of the time of year.
"""

import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

LOCAL_TZ = ZoneInfo("America/New_York")
TARGET_HOURS = {6, 7}  # only actually post if local hour is 6 or 7am

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
}

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
        return c.get("team", {}).get("shortDisplayName") or c.get("team", {}).get("displayName", "TBD")

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


def main():
    now_local = datetime.now(LOCAL_TZ)
    force_run = os.environ.get("FORCE_RUN") == "true"

    # DST-safe gate: only actually post if we're in the target local hour window.
    # (Workflow schedules two UTC triggers to cover both EST and EDT.)
    # Manual "Run workflow" triggers set FORCE_RUN=true to skip this, so you
    # can test at any time of day.
    if not force_run and now_local.hour not in TARGET_HOURS:
        print(f"[skip] local hour is {now_local.hour}, not in {TARGET_HOURS}. Exiting.")
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

    embeds = build_embeds(league_data, today_label)

    # Discord allows max 10 embeds per message
    resp = requests.post(WEBHOOK_URL, json={"embeds": embeds[:10]}, timeout=15)
    if resp.status_code >= 300:
        print(f"[error] Discord webhook failed: {resp.status_code} {resp.text}", file=sys.stderr)
        sys.exit(1)

    print("[ok] Digest posted.")


if __name__ == "__main__":
    main()
