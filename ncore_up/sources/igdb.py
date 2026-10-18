"""IGDB (Twitch) — games on every platform: summary, genres, platforms, companies, release dates, cover.

Needs IGDB_CLIENT_ID + IGDB_CLIENT_SECRET (a Twitch application). The app access token (client credentials) is
cached in ~/.cache/ncore-up/igdb-token.json until it expires. Limit: 4 requests/second.
"""

from __future__ import annotations

import json
import re
import time

from .. import keys, net, workflow

TOKEN_URL = "https://id.twitch.tv/oauth2/token"
API = "https://api.igdb.com/v4/"

# IGDB platform ids for the platforms nCore cares about.
PLATFORMS = {
    "pc": 6, "ps1": 7, "ps2": 8, "ps3": 9, "ps4": 48, "ps5": 167, "psp": 38, "vita": 46, "xbox": 11, "xbox360": 12,
    "xboxone": 49, "xboxseries": 169, "switch": 130, "switch2": 508, "wii": 5, "wiiu": 41, "gamecube": 21, "3ds": 37, "ds": 20,
    "android": 34, "ios": 39,
}
FIELDS = ("name, summary, storyline, first_release_date, genres.name, themes.name, platforms.name, "
          "involved_companies.company.name, involved_companies.developer, involved_companies.publisher, "
          "cover.image_id, url, websites.url, websites.category, release_dates.region, release_dates.platform, alternative_names.name")


def _token_file():
    return net.cache_dir() / "igdb-token.json"


def token() -> str:
    client_id, secret = keys.get("IGDB_CLIENT_ID"), keys.get("IGDB_CLIENT_SECRET")
    if not client_id or not secret:
        return ""
    try:
        cached = json.loads(_token_file().read_text(encoding="utf-8"))
        if cached.get("client_id") == client_id and cached.get("expires", 0) > time.time() + 300:
            return cached["access_token"]
    except (OSError, ValueError, KeyError):
        pass
    data = net.post_json(TOKEN_URL, params={"client_id": client_id, "client_secret": secret, "grant_type": "client_credentials"}, expire=0) or {}
    if not data.get("access_token"):
        return ""
    try:
        workflow.write_json(_token_file(), {"client_id": client_id, "access_token": data["access_token"],
                                          "expires": time.time() + int(data.get("expires_in", 0))})
    except OSError:
        pass
    return data["access_token"]


def query(endpoint: str, body: str) -> list[dict]:
    """Raw APIcalypse query, e.g. query("games", 'search "Total Chaos"; fields name; limit 5;')."""

    access = token()
    if not access:
        return []
    data = net.post_json(API + endpoint, data=body.encode("utf-8"),
                         headers={"Client-ID": keys.get("IGDB_CLIENT_ID"), "Authorization": f"Bearer {access}", "Accept": "application/json"})
    return data if isinstance(data, list) else []


def _quote(text: str) -> str:
    return re.sub(r'["\\]', " ", text).strip()


def search(title: str, platform: str | None = None, limit: int = 5) -> list[dict]:
    where = f" where platforms = ({PLATFORMS[platform]});" if platform in PLATFORMS else ""
    return query("games", f'search "{_quote(title)}"; fields {FIELDS};{where} limit {limit};')


def parse_game(game: dict) -> dict:
    """{"igdb_id", "name", "summary", "year", "genres", "platforms", "developers", "publishers", "cover_url", "url", "websites"}"""

    companies = game.get("involved_companies") or []
    stamp = game.get("first_release_date")
    cover = (game.get("cover") or {}).get("image_id", "")
    return {
        "igdb_id": game.get("id"),
        "name": game.get("name", ""),
        "summary": (game.get("summary") or "").strip(),
        "year": time.strftime("%Y", time.gmtime(stamp)) if stamp else "",
        "genres": [g.get("name", "") for g in game.get("genres") or []],
        "platforms": [p.get("name", "") for p in game.get("platforms") or []],
        "developers": [c["company"]["name"] for c in companies if c.get("developer") and c.get("company")],
        "publishers": [c["company"]["name"] for c in companies if c.get("publisher") and c.get("company")],
        "cover_url": f"https://images.igdb.com/igdb/image/upload/t_cover_big/{cover}.jpg" if cover else "",
        "url": game.get("url", ""),
        "websites": [w.get("url", "") for w in game.get("websites") or []],
    }
