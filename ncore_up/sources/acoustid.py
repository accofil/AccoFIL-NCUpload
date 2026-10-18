"""AcoustID: identify audio by its Chromaprint fingerprint (``ncore_up.local.fingerprint``) -> MusicBrainz
recordings, releases and release groups.

Needs ACOUSTID_KEY (an application key, free for non-commercial use: https://acoustid.org/new-application);
without it every lookup returns []. Limit: 3 requests/s (net paces api.acoustid.org). ``duration`` must be the
whole file's length in seconds.
"""

from __future__ import annotations

from collections import Counter

from .. import keys, net

API = "https://api.acoustid.org/v2/lookup"


def parse_lookup(data: dict) -> list[dict]:
    """[{"id", "score", "recordings": [{"id", "title", "duration", "artists", "release_ids", "release_groups": [{"id", "title", "type"}]}]}]"""

    if not data or data.get("status") != "ok":
        return []
    results = []
    for result in data.get("results") or []:
        recordings = []
        for recording in result.get("recordings") or []:
            release_ids = [r.get("id", "") for r in recording.get("releases") or [] if r.get("id")]
            release_ids += [r for r in recording.get("releaseids") or [] if isinstance(r, str)]
            groups = [{"id": g.get("id", ""), "title": g.get("title", ""), "type": g.get("type", "")} for g in recording.get("releasegroups") or []]
            for group in recording.get("releasegroups") or []:
                release_ids += [r.get("id", "") for r in group.get("releases") or [] if r.get("id")]
            recordings.append({
                "id": recording.get("id", ""), "title": recording.get("title", ""), "duration": float(recording.get("duration") or 0),
                "artists": [a.get("name", "") for a in recording.get("artists") or []], "release_ids": list(dict.fromkeys(release_ids)),
                "release_groups": groups,
            })
        results.append({"id": result.get("id", ""), "score": float(result.get("score") or 0), "recordings": recordings})
    return results


def lookup(fingerprint: str, duration: int) -> list[dict]:
    key = keys.get("ACOUSTID_KEY")
    if not key or not fingerprint or not duration:
        return []
    data = net.post_json(API, data={"client": key, "duration": int(duration), "fingerprint": fingerprint, "format": "json",
                                    "meta": "recordings releaseids releasegroups"})
    return parse_lookup(data or {})


def release_ids(results: list[dict], min_score: float = 0.7) -> set[str]:
    return {rid for result in results if result["score"] >= min_score for rec in result["recordings"] for rid in rec["release_ids"]}


def common_releases(per_track: list[list[dict]], min_score: float = 0.7) -> list[tuple[str, int]]:
    """Release ids ranked by how many of the fingerprinted tracks they contain: [(release id, tracks found)]."""

    counts: Counter = Counter()
    for results in per_track:
        counts.update(release_ids(results, min_score))
    return counts.most_common()
