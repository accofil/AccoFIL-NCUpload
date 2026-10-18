"""Resolvers: one per content group. Each exposes

    resolve(release: ncore_up.release.Release, code: str, *, original: bool, hints: dict) -> ncore_up.meta.Meta

``hints`` may hold user overrides and parsed values. Common: "title", "year", "version", "tags", "kind".
Video: "imdb", "season", "episode", "episodes", "series", "ncore_imdb" (nCore's imdb_movie dict). Music/clip:
"artist", "album", "mbid", "discogs", "narrator". Books: "author", "isbn", "series", "series_index", "publisher",
"proof", "language". Games: "steam", "gog", "deep". Console/mobile: "platform", "package". Software: "platform",
"name", "hash". A resolver combines local readers (``ncore_up.local``)
and sources (``ncore_up.sources``) in a fixed priority chain, sets ``Meta.confidence`` and explains every doubt in
``Meta.warnings``. Hungarian text priority: a native Hungarian source, then ``ncore_up.translate.to_hungarian``
(sets ``machine_translated``), then English.
"""
