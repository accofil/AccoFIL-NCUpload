"""nCore upload rules (wiki: "Feltöltési szabályok") that can be checked from the files before uploading.

Each check returns (errors, warnings). Errors are rule breaks the staff deletes or warns for; warnings are
things worth a look ("törekedni kell", or depends on facts the program cannot know).
"""

from __future__ import annotations

from . import media

LOSSLESS = {"truehd", "mlp", "flac", "pcm_s16le", "pcm_s24le", "pcm_s32le", "pcm_bluray", "alac"}
NEEDS_COMPAT = {"dts", "truehd", "mlp"}  # plus every DTS-HD profile
COMPAT = {"ac3", "eac3", "aac"}
SD_AUDIO = {"aac"}
HD720_AUDIO = {"aac", "ac3", "eac3"}
HD1080_ENCODE_AUDIO = {"aac", "ac3", "eac3", "dts", "flac"}
REMUX_AUDIO = HD1080_ENCODE_AUDIO | {"truehd", "mlp"}
VIDEO_CODECS = {"h264", "hevc"}


def _is_dts_hd(track: dict) -> bool:
    return track["format"] == "dts" and any(x in (track.get("profile") or "") for x in ("HD", "MA", "X", "HRA"))


def video(info: media.VideoInfo, code: str, *, remux: bool, original: bool, foreign_original: bool,
          source: str = "", anime: bool = False) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    foreign_exception = original and foreign_original
    resolution = "sd" if code.startswith(("xvid", "dvd")) else ("2160" if info.height > 1100 or info.width > 1920 else "1080" if info.height > 720 or info.width > 1280 else "720")
    if info.codec and info.codec not in VIDEO_CODECS and not code.startswith("dvd"):
        errors.append(f"csak H.264 vagy H.265 videó tölthető fel (ez: {info.codec})")
    if resolution == "sd":
        if info.width and not code.startswith("dvd") and not 500 <= info.width <= 720:
            errors.append(f"SD kategóriában a szélesség 500–720 pixel lehet (ez: {info.width})")
    if info.codec == "hevc" and resolution in {"sd", "720"}:
        errors.append("x265/H.265 csak 1080p és 2160p kategóriában engedélyezett")
    bluray = "BLURAY" in source.upper()
    if info.codec == "hevc" and resolution == "1080" and not (info.hdr or info.dovi) and not (anime and bluray):
        errors.append("1080p-ben x265/H.265 csak HDR/Dolby Vision videónál engedélyezett")
    if resolution == "2160" and info.codec and info.codec != "hevc":
        errors.append("2160p csak x265/H.265 lehet")
    tracks = 1 + len(info.audio) + len(info.subtitles)
    if tracks > 16 and not foreign_exception:
        errors.append(f"legfeljebb 16 sáv lehet (videó+hang+felirat), ennyi van: {tracks}")
    for kind, items in (("hangsáv", info.audio), ("felirat", info.subtitles)):
        for index, track in enumerate(items, 1):
            if track["lang"] in {"", "und"}:
                errors.append(f"{index}. {kind}: hiányzik a nyelvi címke (language tag kötelező)")
    if code.endswith("_hun") and info.audio:
        hun = [i for i, t in enumerate(info.audio) if t["lang"] in {"hun", "hu"}]
        if not hun:
            errors.append("HUN kategóriában magyar hangsáv szükséges")
        if hun and hun[0] != 0 and not foreign_exception:
            errors.append("a magyar hangsávnak kell az első helyen állnia")
        if hun and not info.audio[hun[0]].get("default") and not foreign_exception:
            errors.append("a magyar hangsáv legyen az alapértelmezett (default)")
        if hun and len(info.audio) == 1:
            warnings.append("HU kategóriában törekedni kell az eredeti hangra is második sávként")
    dubbed = {t["lang"][:2] for t in info.audio}
    for track in info.subtitles:
        if track.get("forced") and track["lang"][:2] not in dubbed:
            errors.append(f"kiegészítő (forced) felirat csak olyan nyelvű lehet, amilyen szinkron van ({track['lang']})")
    formats = [t["format"] for t in info.audio]
    for track in info.audio:
        if abs(float(track.get("delay_ms", 0) or 0)) > 200:
            errors.append("200 ms feletti jelzett hangkésleltetés nem engedélyezett")
    # Wiki 30: foreign originals are exempt only from track count, HU ordering
    # and compatibility audio. Foreign BluRay 1080p may use remux audio formats.
    remux_audio = remux or resolution == "2160" or (foreign_exception and bluray and resolution == "1080")
    allowed = SD_AUDIO if resolution == "sd" and not code.startswith("dvd") else HD720_AUDIO if resolution == "720" else REMUX_AUDIO if remux_audio else HD1080_ENCODE_AUDIO
    mp2_hdtv = original and resolution == "1080" and info.codec == "h264" and "HDTV" in source.upper()
    if mp2_hdtv:
        allowed = allowed | {"mp2"}
    for track in info.audio:
        if code.startswith("dvd"):
            break
        name = "dts-hd" if _is_dts_hd(track) else track["format"]
        if track["format"] not in allowed or (name == "dts-hd" and not remux_audio):
            errors.append(f"nem engedélyezett hangformátum ebben a kategóriában: {name} ({track['lang']})")
        if resolution == "sd" and track.get("channels", 0) > 2:
            errors.append("SD kategóriában legfeljebb 2.0 hang lehet")
        if resolution == "720" and track.get("channels", 0) > 6:
            errors.append("720p-ben legfeljebb 5.1 hang lehet")
        if track["format"] == "flac" and track.get("channels", 0) > 2:
            errors.append("FLAC hang legfeljebb 2.0 lehet")
        if track["format"] == "aac" and track.get("channels", 0) > 2:
            errors.append("AAC hang legfeljebb 2.0 lehet")
    if resolution == "sd" and not code.startswith("dvd") and len(info.audio) > 2:
        errors.append("SD kategóriában legfeljebb 2 hangsáv lehet")
    heavy = [t for t in info.audio if t["format"] in NEEDS_COMPAT or _is_dts_hd(t)]
    if heavy and not foreign_exception and not any(t["format"] in COMPAT for t in info.audio) and not code.startswith("dvd"):
        errors.append("DTS/TrueHD/DTS-HD hang mellé kompatibilitási sáv kötelező (ajánlott DD@640, 2.0-nál DD@256 vagy AAC)")
    if any(t["format"] == "mp2" for t in info.audio) and not mp2_hdtv:
        warnings.append("MP2 hang csak érintetlen 1080i/1080p H.264 HDTV anyagnál engedélyezett")
    return errors, warnings


def sample_needed(code: str, size: int, has_bdmv: bool, original: bool) -> bool:
    """HD films over 2 GB need a ≥1 minute sample cut from the encode (UHD: ≥200 MB). Not for discs and originals."""

    return not original and not has_bdmv and code in {"hd_hun", "hd"} and size > 2 * 1024**3
