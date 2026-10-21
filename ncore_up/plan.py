"""Everything an upload needs, gathered before anything is sent: category, mode, name, form data, files.

Each content group has a resolver (``ncore_up/resolve``) that identifies the content from local files and outside
sources and returns a ``Meta``. This module turns that Meta into the nCore form (``UploadData``), the description
(``compose``), the attachments (NFO, techinfo, screenshots, cover) and the rule checks, and collects every warning
and error for the user — or for the automatic mode, which uploads only plans without errors and doubts (``Plan.doubts``,
e.g. a category the content contradicts) and with enough confidence.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import categories as cats, compose, describe, form as forms, media, net, parse, release as rel, rules, safeio
from .meta import Meta
from . import preparation

# Minimum identification confidence for an automatic upload, per group (the resolvers' own scales).
AUTO_CONFIDENCE = {"video": 0.7, "clip": 0.6, "music": 0.75, "ebook": 0.7, "game": 0.7, "console": 0.7, "mobil": 0.7, "program": 0.7}


@dataclass
class Options:
    category: str | None = None
    original: bool | None = None  # None: automatic (NFO + scene/P2P name)
    imdb: str | None = None
    name: str | None = None
    description_file: str | None = None
    source: str | None = None
    extras: str | None = None
    menu: bool | None = None
    tags: list[str] = field(default_factory=list)
    images: list[str] = field(default_factory=list)
    cover: str | None = None
    screenshots: bool = True
    request_id: str = ""
    other_link: str | None = None
    make_sample: bool = True
    hints: dict = field(default_factory=dict)  # resolver hints from the command line (title, artist, isbn, steam, ...)
    proof_links: list[str] = field(default_factory=list)
    install_guide: str | None = None
    ncup_dir: Path | None = None
    bdinfo: str | None = None


@dataclass
class Plan:
    release: rel.Release
    category: cats.Category
    data: forms.UploadData
    attachments: dict[str, Path] = field(default_factory=dict)  # form field -> local file
    texts: dict[str, str] = field(default_factory=dict)  # form field -> generated file content (techinfo)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    doubts: list[str] = field(default_factory=list)  # not errors, but no unattended upload (e.g. the category may be wrong)
    content: Path | None = None  # what the torrent is made of: the release itself, or a hard-linked staging copy
    meta: Meta | None = None
    confidence: float = 0.0
    requirements: list = field(default_factory=list)
    options: Options | None = None
    video_info: media.VideoInfo | None = None
    excluded_files: set[str] = field(default_factory=set)  # read as metadata, omitted from own torrent payload

    preparation: preparation.Workspace | None = None
    source_release: rel.Release | None = None
    techinfo_files: dict[str, int] = field(default_factory=dict)
    techinfo_digest: str = ""

    @property
    def has_sample(self) -> bool:
        return any("sample" in f.relpath.lower() and f.ext in cats.VIDEO_EXT for f in self.release.files) or (
            self.content is not None and (self.content / "sample.mkv").exists())

    @property
    def content_path(self) -> Path:
        return self.content or self.release.path

    @property
    def auto_ok(self) -> bool:
        """Safe for an unattended upload: no errors, no doubts and a confident identification."""

        return (not self.errors and not self.doubts and all(r.status == "PASS" for r in self.requirements)
                and self.confidence >= AUTO_CONFIDENCE.get(self.category.group, 0.7))

    def stage_dir(self) -> Path:
        """Create a separate NCUP payload, hardlinking or copying across disks."""
        if self.content:
            return self.content
        if self.preparation is None:
            root = self.options.ncup_dir if self.options else None
            self.preparation = preparation.Workspace(self.source_release or self.release, root)
        self.content = self.preparation.stage(self.release, self.excluded_files, rewrite=not self.data.original)
        return self.content


# Scene/P2P names end in "-GROUP"; music scene groups often add an "_iNT"/"_INT" suffix ("…-AUDiAL_iNT").
SCENE_NAME_RE = re.compile(r"^[\w.()&'+,!-]+-[A-Za-z0-9]{2,}(?:_[A-Za-z0-9]{2,})?$")


def looks_original(release: rel.Release) -> bool:
    return release.nfo is not None and bool(SCENE_NAME_RE.match(release.name.replace(" ", ".")))


def build(release: rel.Release, options: Options, client, work_dir: Path) -> Plan:
    # Library callers may use the run-local NCUP; the CLI always supplies the
    # installation's mandatory absolute NCORE_NCUP_DIR setting.
    options.ncup_dir = options.ncup_dir or work_dir.resolve() / "NCUP"
    workspace = preparation.Workspace(release, options.ncup_dir)
    try:
        return _build(release, options, client, work_dir, workspace)
    except media.MediaError as exc:
        workspace.discard()
        raise ValueError(f"média feldolgozása: {exc}") from exc
    except BaseException:
        workspace.discard()  # no plan comes back, so nothing can ever seed from this job
        raise


def _build(release: rel.Release, options: Options, client, work_dir: Path, workspace: preparation.Workspace) -> Plan:
    source_release = release
    guessed = cats.detect(release.name, [f.ext for f in release.files], release.has_bdmv, release.has_video_ts, release.hungarian_audio).code
    if (options.category or guessed or "").startswith("xxx"):
        raise ValueError("XXX kategóriákat ez a program nem tölt fel")
    initial_original = looks_original(release) if options.original is None else options.original
    try:
        release = workspace.expand(release, options.category or guessed, initial_original)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        workspace.discard()
        raise ValueError(f"NCUP előkészítés: {exc}") from exc
    detection = cats.detect(release.name, [f.ext for f in release.files], release.has_bdmv, release.has_video_ts, release.hungarian_audio)
    code = options.category or _local_category(release, detection.code, options.hints)
    if not code:
        raise ValueError("a kategóriát nem tudom megállapítani (" + "; ".join(detection.reasons) + "), add meg: --category " + "|".join(detection.candidates))
    if code not in cats.CATEGORIES:
        raise ValueError(f"ismeretlen kategória: {code}")
    category = cats.CATEGORIES[code]
    if category.group == "xxx":
        raise ValueError("XXX kategóriákat ez a program nem tölt fel")
    original = looks_original(release) if options.original is None else options.original
    if options.original is None and any(op["action"] == "mount-bluray-readonly" for op in workspace.operations):
        original = False
    try:
        release, preparation_errors = workspace.assemble(release, category, original)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        workspace.discard()
        raise ValueError(f"NCUP összeállítás: {exc}") from exc
    plan = Plan(release, category, forms.UploadData(category=code, name=options.name or release.name, original=original))
    plan.options = options
    plan.preparation = workspace
    plan.source_release = source_release
    plan.content = workspace.content
    plan.excluded_files = set(workspace.excluded)
    plan.errors.extend(preparation_errors)
    if workspace.content:
        plan.notes.append(f"NCUP kiadás: {plan.content_path}; jegyzék: {workspace.manifest}")
        if workspace.excluded:
            plan.notes.append("Kihagyott fájlok: " + ", ".join(sorted(workspace.excluded)))
    if code in {"dvd", "dvd_hun"}:
        plan.errors.append("a régi Film/DVD (DVDR) kategóriába tilos feltölteni; DVD5 lemeznél is DVD9 kategória kell (Wiki 30)")
    for item in release.files:
        if item.ext in rel.PARTIAL_EXT:
            plan.errors.append(f"befejezetlen letöltés: {item.relpath}")
    plan.notes.append(f"kategória: {code} ({category.label})" + ("" if options.category else " — " + "; ".join(detection.reasons)))
    plan.notes.append("mód: " + ("EREDETI release (NFO, változatlan név és fájlok)" if original else "nem release (saját feltöltés)"))
    if original:
        for problem in rel.validate_original(release, category):
            (plan.warnings if category.packed_ok and "tömörített" in problem else plan.errors).append(problem)
        if release.nfo:
            plan.attachments["nfo_fajl"] = release.nfo
    else:
        plan.errors.extend(release.problems)
        plan.excluded_files |= {f.relpath for f in release.files if f.ext == ".nfo"}
    work_dir.mkdir(parents=True, exist_ok=True)
    group = category.group
    builder = {"video": _video, "clip": _clip, "music": _music, "ebook": _ebook, "game": _game, "console": _console,
               "mobil": _mobile, "program": _software}.get(group)
    if builder is None:
        raise ValueError(f"ismeretlen kategóriacsoport: {group}")
    builder(plan, options, client, work_dir)
    _common(plan, options, work_dir)
    # NCUP only when the payload differs from the source (archive/ISO extraction, rebuilt structure, a made sample,
    # omitted files); otherwise the torrent is made from and seeded in the source folder itself.
    if forms.needs_techinfo(plan.category.code, plan.data.original):
        from . import techinfo
        try:
            disc = plan.release.has_bdmv and not plan.data.original
            text, covered = techinfo.generate(plan.content_path, plan.texts.get("techinfo_fajl", ""), per_file=not disc)
            plan.texts["techinfo_fajl"] = text
            plan.techinfo_files = covered
            plan.techinfo_digest = techinfo.digest(text)
            plan.notes.append(f"Techinfo: BDInfo-scan lemezenként a fő playlistről ({len(covered)} fájl a torrentben)" if disc
                              else f"Techinfo: a kész torrent mind a(z) {len(covered)} fájlja feldolgozva")
        except (OSError, ValueError, media.MediaError) as exc:
            plan.errors.append(f"teljes techinfo: {exc}")
    from .requirements import evaluate
    evaluate(plan)
    workspace.finalize()
    if plan.errors or plan.doubts:
        workspace.receipt("needs-review")
    return plan


def _local_category(release: rel.Release, detected: str | None, hints: dict) -> str | None:
    """Static signatures before generic ISO/program heuristics; never execute content."""
    if detected and cats.CATEGORIES[detected].group == "xxx":
        return detected
    if detected in {"dvd", "dvd9", "dvd_hun", "dvd9_hun"}:
        return "dvd9" + ("_hun" if detected.endswith("_hun") else "")
    if detected in {"ebook", "ebook_hun"}:
        from .local import ebookmeta
        first = next((f for f in release.files if f.ext in ebookmeta.BOOK_EXT), None)
        language = str(hints.get("language", "")).lower()
        if first and not language:
            path = release.path / first.relpath if release.path.is_dir() else release.path
            language = str(ebookmeta.read(path).get("language", "")).lower()
        if language in {"hu", "hun", "magyar"}:
            return "ebook_hun"
        if language in {"en", "eng", "angol", "de", "deu", "ger", "német"}:
            return "ebook"
    if detected and detected.startswith("xvid"):
        video = rel.main_video(release.path, release.files)
        if video:
            try:
                info = media.probe(video)
                if info.width > 720 or info.height > 576:
                    return ("hdser" if "ser" in detected else "hd") + ("_hun" if detected.endswith("_hun") else "")
            except media.MediaError:
                pass
    if detected:
        return detected
    from .local import gamefiles
    files = gamefiles.classify([(f.relpath, f.size) for f in release.files])
    words = cats.tokens(release.name)
    if hints.get("steam") or hints.get("gog") or files.layout in {"gog", "fitgirl", "dodi", "elamigos"} or files.crack_dirs or words & {"TENOKE", "RUNE", "CODEX", "FLT"}:
        return "game_iso" if files.disc_images else "game_rip"
    # Disc images and PKG are ambiguous (console/game/software), inspect headers.
    if any(f.ext in {".iso", ".bin", ".pkg", ".img"} for f in release.files):
        from .local import consoleid
        console = consoleid.identify(release, hints)
        if console.platform:
            return "console"
        for item in release.files:
            if item.ext in {".iso", ".img"}:
                path = release.path / item.relpath if release.path.is_dir() else release.path
                if media.dvd_image_entries(path):
                    return "dvd9"
    if any(f.ext in {".msi", ".exe", ".msix", ".appx", ".dmg", ".deb", ".rpm"} for f in release.files):
        return "iso" if files.disc_images else "misc"
    if files.disc_images and words & {"WINDOWS", "OFFICE", "LINUX", "UBUNTU", "DEBIAN"}:
        return "iso"
    return None


# --- shared helpers --------------------------------------------------------------------------------------------

def _take_meta(plan: Plan, meta: Meta) -> Meta:
    plan.meta = meta
    plan.confidence = meta.confidence
    plan.sources.extend(s for s in meta.sources if s not in plan.sources)
    plan.warnings.extend(w for w in meta.warnings if w not in plan.warnings)
    if meta.extra.get("nfo_files"):
        plan.notes.append("feldolgozott NFO: " + ", ".join(meta.extra["nfo_files"]))
    if meta.confidence < AUTO_CONFIDENCE.get(plan.category.group, 0.7):
        plan.notes.append(f"azonosítás biztonsága: {meta.confidence:.2f} — automatikus módban ez átnézésre kerül")
    else:
        plan.notes.append(f"azonosítás biztonsága: {meta.confidence:.2f}")
    hint = meta.extra.get("category_hint", "")
    for reason in meta.extra.get("review_reasons", []):
        _doubt(plan, str(reason), str(reason))
    if hint in cats.CATEGORIES and hint != plan.category.code:
        _doubt(plan, f"a tartalom alapján inkább ebbe a kategóriába való: {hint} (most: {plan.category.code})", f"inkább {hint}")
    return meta


def _doubt(plan: Plan, message: str, marker: str) -> None:
    """Something only a human can settle: shown as a warning, and the automatic mode sets the plan aside."""

    plan.doubts.append(message)
    _warn_once(plan, message, marker)


def _cover(plan: Plan, options: Options, meta: Meta | None, work_dir: Path) -> None:
    """Infobar picture (≤300x300): --cover, else the resolver's cover bytes, else its cover URL."""

    if options.cover or meta is None:
        return
    image = meta.cover
    if not image and meta.cover_url:
        if net.robots_allowed(meta.cover_url):
            image = net.get_bytes(meta.cover_url)
        else:
            plan.warnings.append(f"a borító helyének robots.txt-je tiltja a letöltést, kihagytam: {meta.cover_url}")
    if not image:
        return
    try:
        plan.attachments["infobar_kep"] = media.infobar_cover(image, work_dir / "borito.jpg")
    except media.MediaError as exc:
        plan.warnings.append(f"borító: {exc}")


def _hints(options: Options, **extra) -> dict:
    hints = {k: v for k, v in extra.items() if v not in (None, "", [])}
    hints.update({k: v for k, v in options.hints.items() if v not in (None, "", [])})
    if options.tags:
        hints["tags"] = options.tags
    return hints


def _screenshots(plan: Plan, options: Options, stream: Path | None, work_dir: Path) -> None:
    if not stream or not options.screenshots or options.images or not stream.is_file():
        return
    try:
        for index, shot in enumerate(media.screenshots(stream, work_dir / "kepek"), 1):
            plan.attachments[f"kep{index}"] = shot
    except media.MediaError as exc:
        needed = forms.needs_screenshots(plan.category.code, plan.data.original, plan.has_sample)
        (plan.errors if needed else plan.warnings).append(f"képek: {exc}")


def _video_files(release: rel.Release, work_dir: Path | None = None):
    target = media.techinfo_target(release.path, release.files, release.has_bdmv)
    if target is None and work_dir is not None and not release.has_video_ts:
        image = next((f for f in release.files if f.ext in {".iso", ".img"}), None)
        if image:
            path = release.path / image.relpath if release.path.is_dir() else release.path
            try:
                target = media.dvd_image_stream(path, work_dir / "dvd-analysis")
            except media.MediaError:
                pass  # requirement video_probe remains UNKNOWN
    stream = media.disc_main_stream(release.path) if (release.has_bdmv or release.has_video_ts) else target
    info = None
    if stream and Path(stream).is_file():
        try:
            info = media.probe(Path(stream))
        except media.MediaError:
            info = None
    return target, stream, info


def _check_all_videos(plan: Plan, main_info) -> None:
    release = plan.release
    if plan.category.group == "video" and plan.category.code not in forms.SERIES_CODES and len([f for f in release.files if f.ext in {".iso", ".img"}]) > 1:
        _doubt(plan, "több filmlemezkép van: filmcsomag vagy többlemezes kiadás ellenőrzése szükséges", "több filmlemezkép")
    if release.has_bdmv or release.has_video_ts:
        return  # disc playlist selection is handled by BDInfo/disc_main_stream
    main = [f for f in release.files if f.ext in cats.VIDEO_EXT and f.top_dir not in rel.COMPANION_DIRS
            and "sample" not in Path(f.relpath).stem.lower()]
    if plan.category.group == "video" and plan.category.code not in forms.SERIES_CODES and len(main) > 1:
        plan.errors.append("egy film lehet egy torrentben: több fő videófájl van (filmcsomag / felosztott film)")
    for item in main:
        path = release.path / item.relpath if release.path.is_dir() else release.path
        try:
            info = main_info if main_info and Path(main_info.path) == path else media.probe(path)
        except media.MediaError:
            _doubt(plan, f"nem ellenőrizhető videó: {item.relpath}", item.relpath)
            continue
        if not info.codec or not info.width or not info.height or info.duration <= 0:
            _doubt(plan, f"hiányos videóelemzés: {item.relpath}", item.relpath)
        if plan.category.group == "video" and not release.has_bdmv:  # an untouched full disc keeps its authored tracks
            errors, warnings = rules.video(info, plan.category.code, remux="REMUX" in cats.tokens(release.name),
                                           original=plan.data.original, foreign_original=plan.data.original and not plan.category.code.endswith("_hun"),
                                           source=release.name, anime=bool(plan.options and plan.options.hints.get("anime")))
            plan.errors.extend(errors)
            plan.warnings.extend(warnings)


# --- film, series --------------------------------------------------------------------------------------------

def _video(plan: Plan, options: Options, client, work_dir: Path) -> None:
    from .resolve import video

    release, code, data = plan.release, plan.category.code, plan.data
    target, stream, info = _video_files(release, work_dir)
    plan.video_info = info
    _check_all_videos(plan, info)
    main_file = Path(target).name if target else release.name
    parsed = parse.parse(release.name if release.path.is_dir() else main_file)
    series = code in forms.SERIES_CODES
    hungarian = code.endswith("_hun")
    hints = _hints(options, imdb=options.imdb or release.imdb_id, title=parsed.title, year=parsed.year, season=parsed.season,
                   episode=parsed.episode if parsed.episode and not parsed.episode_last else None,
                   episodes=parsed.episodes or (_episode_span(release) if series else ""), series=series)
    meta = video.resolve(release, code, original=data.original, hints=hints)
    imdb_id = meta.identifiers.get("imdb", "")
    if imdb_id and client is not None:
        try:
            ncore_data = client.imdb_movie(imdb_id)
        except Exception:  # noqa: BLE001 - nCore's IMDb helper is only a cross-check
            ncore_data = {}
        if ncore_data:
            meta = video.resolve(release, code, original=data.original, hints={**hints, "imdb": imdb_id, "ncore_imdb": ncore_data})
    _take_meta(plan, meta)
    if not meta.identifiers.get("imdb") and not (options.other_link or meta.extra.get("other_link")):
        plan.errors.append("nem találtam IMDb-azonosítót: add meg --imdb tt... (vagy --other-link)")

    data.infobar = forms.Infobar(
        imdb_id=meta.identifiers.get("imdb", ""), other_link=options.other_link if options.other_link is not None else meta.extra.get("other_link", ""),
        year=meta.year, countries=[c for c in meta.extra.get("countries", "").split(", ") if c], runtime=meta.extra.get("runtime", ""),
        title_hu=meta.title_hu, title_en=meta.title, directors=meta.creators[:3], cast=[c for c in meta.extra.get("cast", "").split(", ") if c],
    )
    if data.infobar.title_hu.strip().casefold() == data.infobar.title_en.strip().casefold():
        data.infobar.title_hu = ""
    plot = "\n".join(compose.plot_block(meta))
    if not data.original:
        data.name = options.name or describe.video_name(
            meta.title_hu, meta.title or parsed.title, release.name, hungarian=hungarian or bool(meta.title_hu),
            disc=_disc_label(release, info), season=parsed.season if series else None,
            episodes=f"{parsed.episode:02d}" if series and parsed.episode and not parsed.episode_last else "",
            sd=code.startswith("xvid"), hdr=bool(info and info.hdr), dovi=bool(info and info.dovi),
            uhd_bluray=release.has_bdmv and bool(info and info.height > 1100),
        )
        bar = data.infobar
        bar.title_other = (meta.title_original or meta.title) if _named_after(data.name, meta.title_hu) else meta.title_hu
        if series:
            data.season = parsed.season
            data.episodes = parsed.episodes or _episode_span(release)
            data.episode_title = meta.extra.get("episode_title", "")
        external = [describe.language(m.group(1)) + " (srt)" for f in release.files
                    for m in [re.search(r"[._-]([a-z]{2,3})(?:[._-](?:forced|sdh))?\.(?:srt|ass|sup)$", f.relpath, re.I)] if m]
        data.audio = describe.audio_line(info.audio) if info else ""
        data.subtitles = describe.subtitle_line(info.subtitles, external) if info else ""
        is_disc = release.has_bdmv or release.has_video_ts or code.startswith("dvd")
        data.extras = options.extras if options.extras is not None else ("ismeretlen" if is_disc else "nincs")
        data.menu = options.menu if options.menu is not None else release.bd_menu if is_disc else False
        body = describe.video_description(
            menu=data.menu if code in forms.DISC_FILM_CODES or is_disc else None, audio=info.audio if info else [],
            subtitles=info.subtitles if info else [], external_subs=external, extras=data.extras,
            plot=plot, source=options.source or "ismeretlen",
            include_details=not forms.renders_video_details(code, data.original),
        )
        data.description = body
        _video_checks(plan, info, release)
    else:
        data.description = (plot + "\n") if meta.description_hu else ""
    if info and not release.has_video_ts and not release.has_bdmv:  # full discs: BDInfo + menu rules, not encode rules
        errors, warnings = rules.video(info, code, remux=bool(re.search(r"(?i)\bremux\b", release.name)), original=data.original,
                                       foreign_original=data.original and not hungarian, source=release.name, anime=bool(options.hints.get("anime")))
        plan.errors.extend(errors)
        plan.warnings.extend(warnings)
    sample = None
    if not data.original and rules.sample_needed(code, release.size, release.has_bdmv, data.original):
        sample = _sample(plan, options, release, stream, info, work_dir)
    if not data.original and target and (release.has_bdmv or Path(target).suffix.lower() == ".vob"):
        _techinfo(plan, target, release, work_dir)
    _screenshots(plan, options, Path(stream) if stream else None, work_dir)
    _cover(plan, options, meta, work_dir)


def _named_after(name: str, title: str) -> bool:
    """The torrent name starts with ``title`` (video_name writes "A: B" as "A - B")."""

    shown = re.sub(r"\s*:\s*", " - ", title).strip().lower()
    return bool(shown) and name.lower().startswith(shown)


def _disc_roots(release: rel.Release) -> dict[Path, int]:
    """Each Blu-ray disc of a release (the folder holding BDMV) with its size: one for a single disc, several for a
    season pack of Disc1..DiscN folders."""

    roots: dict[Path, int] = {}
    for item in release.files:
        parts = Path(item.relpath).parts
        upper = [p.upper() for p in parts]
        if "BDMV" in upper:
            root = Path(*parts[:upper.index("BDMV")])
        elif len(parts) > 1 and parts[-1].casefold() == "disc.inf":
            root = Path(*parts[:-1])
        else:
            continue
        roots[root] = roots.get(root, 0) + item.size
    for item in release.files:  # AACS/CERTIFICATE/ANY! trees belong to the disc beside its BDMV
        parts = Path(item.relpath).parts
        for root in roots:
            if parts[:len(root.parts)] == root.parts and "BDMV" not in [p.upper() for p in parts] \
                    and parts[-1].casefold() != "disc.inf":
                roots[root] += item.size
    return roots


def _disc_label(release: rel.Release, info: media.VideoInfo | None) -> str:
    if release.has_bdmv:
        # BD25/BD50/... describes one disc: a multi-disc pack is labelled by its largest disc
        largest = max(_disc_roots(release).values(), default=release.size)
        return describe.disc_label(largest, uhd=bool(info and info.height > 1100))
    if release.has_video_ts or any(f.ext == ".iso" for f in release.files):
        return "DVD9" if release.size > 4.7e9 else "DVD5"
    return ""


def _episode_span(release: rel.Release) -> str:
    numbers = sorted({int(m.group(1)) for f in release.files for m in [re.search(r"(?i)S\d{1,3}E(\d{1,4})", f.relpath)] if m})
    if not numbers:
        return ""
    return str(numbers[0]) if len(numbers) == 1 else f"{numbers[0]}-{numbers[-1]}"


def _video_checks(plan: Plan, info: media.VideoInfo | None, release: rel.Release) -> None:
    """nCore wiki rules for non-release videos."""

    code = plan.category.code
    for item in release.files:
        if item.relpath in plan.excluded_files:
            continue
        if not release.has_bdmv and item.ext in {".ts", ".m2ts"}:
            plan.errors.append(f".ts/.m2ts fájl nem lehet a feltöltésben: {item.relpath}")
            continue
        if item.ext in {".vob", ".ifo", ".bup"} and "sample" not in item.relpath.lower():
            plan.errors.append(f"DVD csak lemezképként (iso, bin-cue, mds-mdf) tölthető fel, VOB nem: {item.relpath}")
            break
        allowed = {".mkv", ".srt", ".sub", ".idx"} | cats.IMAGE_EXT
        if code.startswith(("dvd", "dvdser")):
            allowed = cats.DISC_IMAGE_EXT | cats.IMAGE_EXT | ({".mkv"} if "sample" in item.relpath.lower() else set())
        if not release.has_bdmv and not release.has_video_ts and item.ext not in allowed:
            plan.errors.append(f"nem release-ben nem lehet ilyen fájl: {item.relpath} (megengedett: videó, sample, .srt, .sub, borítókép)")
    if any(f.ext == ".mp4" for f in release.files) and code in {"xvid_hun", "xvid", "xvidser_hun", "xvidser"}:
        plan.errors.append("SD kategóriában .mp4 csak scene release-nél lehet")
    if release.has_bdmv and plan.data.menu is False:
        plan.errors.append("BluRay lemez csak menüvel tölthető fel")


def _sample(plan: Plan, options: Options, release: rel.Release, stream, info: media.VideoInfo | None, work_dir: Path) -> Path | None:
    """The wiki: films over 2 GB need a ≥1 minute sample cut from the encode (UHD ≥200 MB) — find or make one."""

    uhd = bool(info and info.height > 1100)
    samples = [release.path / f.relpath for f in release.files if "sample" in f.relpath.lower() and f.ext in cats.VIDEO_EXT] if release.path.is_dir() else []
    if samples:
        sample = samples[0]
        try:
            sample_info = media.probe(sample)
        except media.MediaError:
            sample_info = None
        if sample_info and sample_info.duration < 60:
            plan.errors.append(f"a sample rövidebb 1 percnél ({sample_info.duration:.0f} mp)")
        if sample_info is None:
            _doubt(plan, "a kötelező sample technikai adatai nem olvashatók", "sample technikai")
        if uhd and sample.stat().st_size < 200 * 1024**2:
            plan.errors.append("UHD-nél a sample legalább 200 MB kell legyen")
        return sample
    if not options.make_sample:
        plan.errors.append("2 GB fölötti filmhez legalább 1 perces sample kell (UHD: ≥200 MB) — nincs (--no-make-sample miatt nem vágtam)")
        return None
    if stream is None:
        _doubt(plan, "a kötelező sample nem készíthető el: hiányzó videó", "sample nem készíthető")
        return None
    try:
        stage = plan.stage_dir()
        sample = media.make_sample(Path(stream), stage / "sample.mkv", min_bytes=200 * 1024**2 if uhd else 0)
    except (media.MediaError, ValueError) as exc:
        plan.errors.append(str(exc))
        return None
    plan.notes.append(f"sample elkészítve (hardlinkes másolatba): {sample}")
    return sample


def _techinfo(plan: Plan, target, release: rel.Release, work_dir: Path) -> None:
    try:
        if release.has_bdmv:
            tool = (plan.options.bdinfo if plan.options else None) or str(_bdinfo())
            base = release.path if release.path.is_dir() else Path(target)
            roots = sorted(_disc_roots(release)) or [Path(".")]
            reports = []
            for root in roots:
                report = media.bdinfo_text(tool, base / root, work_dir)
                reports.append(report if len(roots) == 1 else f"===== {root.as_posix()} =====\n\n{report}")
            text = "\n\n".join(reports)
        else:
            path = Path(target) if Path(target).is_file() else media.disc_main_stream(release.path)
            if path is None:
                raise media.MediaError("nincs videófájl a techinfóhoz")
            videos = [release.path / f.relpath for f in release.files if f.ext in cats.VIDEO_EXT
                      and f.relpath not in plan.excluded_files] if release.path.is_dir() else []
            text = "\n\n".join(media.mediainfo_text(p) for p in videos) if videos else media.mediainfo_text(path)
    except media.MediaError as exc:
        plan.errors.append(f"techinfo: {exc}")
        return
    plan.texts["techinfo_fajl"] = text


def _bdinfo() -> Path:
    return Path.home() / "Upload-Assistant" / "bin" / "bdinfo" / "linux" / "amd64" / "bdinfo"


# --- clip (concerts, music videos) ---------------------------------------------------------------------------

def _clip(plan: Plan, options: Options, client, work_dir: Path) -> None:
    from .resolve import clip

    release, data = plan.release, plan.data
    target, stream, info = _video_files(release, work_dir)
    plan.video_info = info
    _check_all_videos(plan, info)
    parsed = parse.parse(release.name)
    meta = _take_meta(plan, clip.resolve(release, plan.category.code, original=data.original, hints=_hints(options, year=parsed.year)))
    data.artist, data.album, data.album_year = ", ".join(meta.creators), meta.title, meta.year
    data.styles = meta.tags or ["live"]
    if not data.original:
        disc = _disc_label(release, info) if (release.has_bdmv or release.has_video_ts) else ""
        artists = "VA" if len(meta.creators) > 3 else ", ".join(meta.creators)
        base = f"{artists} - {meta.title}".strip(" -") or release.name
        data.name = options.name or meta.extra.get("torrent_name") or " ".join(x for x in (base, disc or parsed.resolution) if x)
        external = []
        data.description = compose.clip(
            meta, audio=describe.audio_line(info.audio) if info else "", subtitles=describe.subtitle_line(info.subtitles, external) if info else "",
            menu=(release.bd_menu if release.has_bdmv or release.has_video_ts else False) if options.menu is None else options.menu,
            extras=options.extras if options.extras is not None else ("ismeretlen" if release.has_bdmv or release.has_video_ts else "nincs"),
            chapters=bool(info and info.chapters), sample=plan.has_sample, source=options.source or "ismeretlen")
        if not meta.tracks:
            plan.warnings.append("klip: nem találtam tracklistát (fejezetek / MusicBrainz / Discogs / setlist.fm) — írd be (--edit)")
        if info and not release.has_video_ts:
            errors, warnings = rules.video(info, "hd" if (info.height or 0) > 576 else "xvid", remux=False, original=False, foreign_original=False)
            plan.warnings.extend(f"klip: {w}" for w in errors + warnings)  # clip rules are looser than film rules: only shown
        if target and (release.has_bdmv or Path(target).suffix.lower() == ".vob"):
            _techinfo(plan, target, release, work_dir)
    _screenshots(plan, options, Path(stream) if stream else None, work_dir)
    _cover(plan, options, meta, work_dir)


# --- music, audiobooks -----------------------------------------------------------------------------------------

def _music(plan: Plan, options: Options, client, work_dir: Path) -> None:
    from .resolve import music

    release, data = plan.release, plan.data
    meta = _take_meta(plan, music.resolve(release, plan.category.code, original=data.original, hints=_hints(options)))
    audiobook = meta.kind == "audiobook"
    # audiobooks: the narrator is the performer; without one the author keeps the required field filled
    data.artist = (meta.extra.get("narrator", "") if audiobook else "") or ", ".join(meta.creators)
    data.album, data.album_year = meta.title, meta.year
    data.styles = meta.tags or (["hangoskönyv"] if audiobook else [])
    if not data.styles and not options.tags:
        plan.warnings.append("nincs stílus-címke (add meg: --tags rock,pop)")
    data.description = compose.audiobook(meta) if audiobook else compose.music(meta)
    if not meta.tracks:
        plan.warnings.append("nem sikerült számlistát kinyerni a helyi adatokból vagy a zenei adatbázisokból")
    if not data.original:
        if audiobook:
            name = f"{', '.join(meta.creators[:3])} - {meta.title}".strip(" -")
        else:
            artists = "VA" if len(meta.creators) > 3 or any(c.lower() in {"various artists", "various", "va"} for c in meta.creators) else ", ".join(meta.creators)
            name = f"{artists} - {meta.title}".strip(" -")
        data.name = options.name or meta.extra.get("torrent_name") or name or release.name
        lossy = meta.extra.get("bitrate_kbps", "")
        if lossy.isdigit() and int(lossy) < 128 and plan.category.code in {"mp3", "mp3_hun"}:
            message = f"MP3 kategóriában legalább 128 kbps kell (a leggyengébb szám: {lossy} kbps)"
            plan.errors.append(message)
            plan.warnings[:] = [w for w in plan.warnings if "128 kbps" not in w]
        _music_files(plan)
        if not meta.proof_links:
            _warn_once(plan, "nincs igazoló link (MusicBrainz/Discogs): a szabályzat kötelezővé teszi — írd be (--edit)", "igazoló link")
    _cover(plan, options, meta, work_dir)


def _music_files(plan: Plan) -> None:
    """Recheck the assembled payload against the same central Wiki rules."""
    from .payload import decision
    for item in plan.release.files:
        if item.relpath in plan.excluded_files:
            continue
        action, reason = decision(plan.release, item, plan.category, False)
        if action != "KEEP":
            plan.errors.append(f"{reason}: {item.relpath}")


def _warn_once(plan: Plan, message: str, marker: str) -> None:
    """Add a warning unless the resolver already said the same thing in its own words (``marker`` in it)."""

    if not any(marker in w for w in plan.warnings):
        plan.warnings.append(message)


def _first_audio(release: rel.Release) -> Path | None:
    exts = cats.MP3_EXT | cats.LOSSLESS_EXT | {".m4b", ".ogg", ".opus"}
    files = sorted(f.relpath for f in release.files if f.ext in exts)
    if not files:
        return None
    return release.path / files[0] if release.path.is_dir() else release.path


# --- ebook, comic, magazine ------------------------------------------------------------------------------------

def _ebook(plan: Plan, options: Options, client, work_dir: Path) -> None:
    from .local import ebookmeta
    from .resolve import books

    release, data = plan.release, plan.data
    meta = _take_meta(plan, books.resolve(release, plan.category.code, original=data.original, hints=_hints(options)))
    if meta.languages and ("magyar" in meta.languages) != plan.category.code.endswith("_hun"):
        _doubt(plan, f"e-könyv: a nyelv ({', '.join(meta.languages)}) nem illik a kategóriához ({plan.category.code})", "kategóriát választottál")
    data.book_title, data.book_year, data.book_format = meta.title, meta.year, meta.extra.get("format", "")
    data.styles = meta.tags
    if not data.styles and not options.tags:
        plan.warnings.append("e-könyv: nincs stílus-címke (--tags), pl. szépirodalom, sci-fi, szakkönyv")
    if not data.original:
        data.name = options.name or meta.extra.get("torrent_name") or release.name
        data.description = compose.book(meta)
        main_file = meta.extra.get("main_file", "")
        if main_file and not options.images:
            pages = ebookmeta.proof_pages(Path(main_file), work_dir / "kepek", warnings=plan.warnings, layer=meta.extra.get("text_layer", ""))
            for index, page in enumerate(pages[:3], 1):
                plan.attachments[f"kep{index}"] = page
        if not meta.proof_links:
            _warn_once(plan, "e-könyv: nincs igazoló link — a szabályzat kötelezővé teszi (--edit)", "igazoló link")
    _cover(plan, options, meta, work_dir)


# --- games, console, mobile, programs ----------------------------------------------------------------------------

def _soft_common(plan: Plan, options: Options, meta: Meta, work_dir: Path, render) -> None:
    data = plan.data
    if not data.original:
        data.name = options.name or meta.extra.get("torrent_name") or plan.release.name
        data.description = render(meta, data.name)
        if not meta.install_hu:
            _warn_once(plan, "nincs telepítési útmutató: a szabályzat magyar nyelvű útmutatót kér (--edit)", "telepítési útmutató")
        if not meta.proof_links:
            _warn_once(plan, "nincs igazoló link / hivatalos oldal: a szabályzat kötelezővé teszi (--edit)", "igazoló link")
    _cover(plan, options, meta, work_dir)


def _game(plan: Plan, options: Options, client, work_dir: Path) -> None:
    from .resolve import games

    meta = _take_meta(plan, games.resolve(plan.release, plan.category.code, original=plan.data.original, hints=_hints(options)))
    _soft_common(plan, options, meta, work_dir, compose.game)


def _console(plan: Plan, options: Options, client, work_dir: Path) -> None:
    from .resolve import console

    meta = _take_meta(plan, console.resolve(plan.release, plan.category.code, original=plan.data.original, hints=_hints(options)))
    _soft_common(plan, options, meta, work_dir, compose.console)


def _mobile(plan: Plan, options: Options, client, work_dir: Path) -> None:
    from .resolve import mobile

    meta = _take_meta(plan, mobile.resolve(plan.release, plan.category.code, original=plan.data.original, hints=_hints(options)))
    _soft_common(plan, options, meta, work_dir, compose.app)


def _software(plan: Plan, options: Options, client, work_dir: Path) -> None:
    from .resolve import software

    meta = _take_meta(plan, software.resolve(plan.release, plan.category.code, original=plan.data.original, hints=_hints(options)))
    _soft_common(plan, options, meta, work_dir, compose.software)


# --- shared --------------------------------------------------------------------------------------------------

def _common(plan: Plan, options: Options, work_dir: Path) -> None:
    data = plan.data
    data.request_id = options.request_id
    if options.description_file:
        try:
            data.description = Path(options.description_file).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            plan.errors.append(f"--description: nem olvasható ({exc})")
    if options.proof_links:
        data.description += "\nIgazoló link:\n" + "\n".join(options.proof_links) + "\n"
    if options.install_guide:
        try:
            data.description += "\nTelepítés:\n" + Path(options.install_guide).read_text(encoding="utf-8") + "\n"
        except (OSError, UnicodeDecodeError) as exc:
            plan.errors.append(f"telepítési útmutató: {exc}")
    data.description = forms.description_text(data)
    if plan.excluded_files:
        try:
            plan.stage_dir()
            plan.notes.append("Metaadat/kísérőfájl feldolgozva, a saját torrent fájllistájából kihagyva: " + ", ".join(sorted(plan.excluded_files)))
        except ValueError as exc:
            plan.errors.append(str(exc))
    if options.cover:
        try:
            plan.attachments["infobar_kep"] = media.infobar_cover(safeio.read_file(options.cover, safeio.IMAGE_LIMIT), work_dir / "borito.jpg")
        except (OSError, ValueError, media.MediaError) as exc:
            plan.errors.append(f"borító: {exc}")
    if options.images:
        if len(options.images) != 3:
            plan.errors.append("--images: pontosan 3 képet adj meg")
        for index, image in enumerate(options.images[:3], 1):
            if not Path(image).is_file():
                plan.errors.append(f"--images: nincs ilyen fájl: {image}")
            plan.attachments[f"kep{index}"] = Path(image)
    if options.tags:
        data.styles = options.tags
    if data.original and data.name != plan.release.name:
        plan.warnings.append("eredeti release-nél a torrent neve a release neve kell legyen")
    if net.FAILURES:
        plan.notes.append("elérhetetlen források: " + ", ".join(sorted(set(net.FAILURES))[:8]))
    # per-track rule checks repeat themselves (e.g. two 5.1 tracks in SD): every message once, in order
    plan.warnings[:] = list(dict.fromkeys(plan.warnings))
    plan.errors[:] = list(dict.fromkeys(plan.errors))
