from __future__ import annotations

import importlib.util
import json
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from ncore_up.local import audiotags, chapters, cuelog, fingerprint
from ncore_up.release import ReleaseFile

FIX = Path(__file__).parent / "fixtures" / "music"
HAS_MUTAGEN = importlib.util.find_spec("mutagen") is not None

# MusicBrainz disc 8jTZJTgjGGtKjwdt8UyAgK3DnbY- (Pink Floyd, Dark Side of the Moon, EMI 1994 CD)
DSOTM_OFFSETS = [183, 18018, 34143, 66003, 87533, 116218, 151488, 166913, 184200]
DSOTM_LEADOUT = 193293
DSOTM_DISCID = "8jTZJTgjGGtKjwdt8UyAgK3DnbY-"


def msf(frames: int) -> str:
    return f"{frames // 4500:02d}:{frames // 75 % 60:02d}:{frames % 75:02d}"


class AudioTagsTests(unittest.TestCase):
    def test_normalize_aliases(self):
        tags = audiotags.normalize({"ALBUM ARTIST": ["X"], "MusicBrainz Album Id": ["89BD26A2-4D46-334F-91C2-5E8DF803DAB3"],
                                    "TOTALTRACKS": "10", "Catalog #": "ABC 1", "ORGANIZATION": "Label", "upc": b"0 77774 600125",
                                    "----:com.apple.iTunes:MusicBrainz Release Group Id": ["40bb33d4-0def-3faf-ba17-c94cbee5b246"]})
        self.assertEqual(tags["albumartist"], ["X"])
        self.assertEqual(tags["tracktotal"], ["10"])
        self.assertEqual(tags["catalognumber"], ["ABC 1"])
        self.assertEqual(tags["label"], ["Label"])
        item = audiotags.from_tags(Path("a.flac"), tags)
        self.assertEqual(item.mb_album_id, "89bd26a2-4d46-334f-91c2-5e8df803dab3")
        self.assertEqual(item.mb_release_group_id, "40bb33d4-0def-3faf-ba17-c94cbee5b246")
        self.assertEqual(item.barcode, "077774600125")
        self.assertEqual(item.track_total, 10)

    def test_from_tags_numbers_genres_narrator(self):
        item = audiotags.from_tags(Path("x.mp3"), audiotags.normalize({
            "tracknumber": "3/10", "discnumber": "2/2", "genre": "Rock; Pop/Beat", "comment": "Kiadó: X\nFelolvassa: Kaszás Attila", "date": "1969-03-01"}))
        self.assertEqual((item.track, item.track_total, item.disc, item.disc_total), (3, 10, 2, 2))
        self.assertEqual(item.genres, ["Rock", "Pop", "Beat"])
        self.assertEqual(item.narrator, "Kaszás Attila")
        self.assertEqual(item.year, "1969")

    def test_parse_ffprobe(self):
        data = json.loads((FIX / "ffprobe_mp3.json").read_text(encoding="utf-8"))
        item = audiotags.parse_ffprobe(Path("01.mp3"), data)
        self.assertEqual((item.artist, item.albumartist, item.album, item.title), ("Omega", "Omega", "10 000 lépés", "Petróleum lámpa"))
        self.assertEqual((item.track, item.track_total, item.codec, item.sample_rate), (1, 10, "mp3", 44100))
        self.assertAlmostEqual(item.duration, 201.04, places=1)
        self.assertEqual(item.bitrate // 1000, 100)

    def test_parse_filename(self):
        self.assertEqual(audiotags.parse_filename("01 - Petróleum lámpa.mp3")["title"], "Petróleum lámpa")
        parsed = audiotags.parse_filename("1-03. Pink Floyd - Time.flac")
        self.assertEqual((parsed["disc"], parsed["number"], parsed["artist"], parsed["title"]), (1, 3, "Pink Floyd", "Time"))
        self.assertEqual(audiotags.parse_filename("07 - 10000 lépés.flac")["title"], "10000 lépés")
        self.assertEqual(audiotags.parse_filename("03_kerek_egesz.mp3")["title"], "kerek egesz")
        self.assertEqual(audiotags.parse_filename("1979.mp3")["number"], 0)

    def test_format_label_and_bitrate(self):
        def item(**kw):
            return audiotags.AudioTags(path=Path("x"), **kw)

        self.assertEqual(audiotags.format_label([item(codec="flac", bits_per_sample=16, sample_rate=44100)] * 3), "FLAC 16/44.1")
        self.assertEqual(audiotags.format_label([item(codec="flac", bits_per_sample=24, sample_rate=96000)]), "FLAC 24/96")
        self.assertEqual(audiotags.format_label([item(codec="mp3", bitrate=320000, bitrate_mode="CBR")] * 2), "MP3 320 kbps CBR")
        self.assertEqual(audiotags.format_label([item(codec="mp3", bitrate=245000, bitrate_mode="VBR", encoder_settings="-V 0")]), "MP3 VBR V0")
        self.assertEqual(audiotags.format_label([item(codec="mp3", bitrate=240000, bitrate_mode="VBR"), item(codec="mp3", bitrate=250000, bitrate_mode="VBR")]),
                         "MP3 VBR ~245 kbps")
        self.assertEqual(audiotags.format_label([item(codec="aac", bitrate=256000), item(codec="flac", bits_per_sample=16, sample_rate=48000)]),
                         "AAC 256 kbps + FLAC 16/48")
        self.assertEqual(audiotags.lowest_bitrate_kbps([item(codec="mp3", bitrate=320000), item(codec="mp3", bitrate=192000),
                                                        item(codec="flac", bitrate=900000)]), 192)
        self.assertEqual(audiotags.lowest_bitrate_kbps([item(codec="flac", bitrate=900000)]), 0)

    def test_audio_files_cover_and_disc_folders(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            names = ["CD2/01 - b.flac", "CD1/10 - j.flac", "CD1/2 - a.flac", "Sample/sample.flac", "back.jpg", "Cover.JPG", "x.log"]
            files = []
            for name in names:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"x" * (50 if name == "back.jpg" else 10))
                files.append(ReleaseFile(name, path.stat().st_size))
            found = audiotags.audio_files(root, files)
            self.assertEqual([p.relative_to(root).as_posix() for p in found], ["CD1/2 - a.flac", "CD1/10 - j.flac", "CD2/01 - b.flac"])
            self.assertEqual(audiotags.find_cover_file(root, files).name, "Cover.JPG")
            self.assertEqual(audiotags.disc_from_path(found[2]), 2)
            self.assertEqual(audiotags.disc_from_path(root / "Disc 3" / "x.flac"), 3)
            self.assertEqual(audiotags.disc_from_path(root / "Album" / "x.flac"), 0)

    def test_audio_files_skip_appledouble_keep_sampler(self):
        files = [ReleaseFile(n, 10) for n in ("._01 - a.flac", "01 - a.flac", "02 - Sampler.flac", "03 - Free Sample.flac", "grp-sample.flac")]
        with tempfile.TemporaryDirectory() as tmp:
            found = [p.name for p in audiotags.audio_files(Path(tmp), files)]
        self.assertEqual(found, ["01 - a.flac", "02 - Sampler.flac", "03 - Free Sample.flac"])

    def test_parse_ffprobe_odd_numbers(self):
        data = {"format": {"duration": "N/A", "bit_rate": "N/A", "tags": {"title": "x"}},
                "streams": [{"codec_type": "audio", "codec_name": "flac", "sample_rate": "44100", "bits_per_raw_sample": "N/A",
                             "duration": "N/A", "channels": 2}]}
        item = audiotags.parse_ffprobe(Path("x.flac"), data)
        self.assertEqual((item.duration, item.bitrate, item.sample_rate, item.bits_per_sample, item.title), (0.0, 0, 44100, 0, "x"))

    def test_flac_cuesheet_uses_index_01(self):
        def track(number, start, indexes):
            return SimpleNamespace(track_number=number, start_offset=start, type=0,
                                   indexes=[SimpleNamespace(index_number=n, index_offset=o) for n, o in indexes])

        # track 2 starts at its INDEX 00 (pregap); its INDEX 01 is 88200 samples (2 s) later
        cue = SimpleNamespace(tracks=[track(1, 0, [(1, 0)]), track(2, 441000, [(0, 0), (1, 88200)]), track(170, 1323000, [])])
        audio = SimpleNamespace(tags=None, pictures=[], cuesheet=cue)
        _raw, _cover, _mime, toc = audiotags._mutagen_raw(audio)
        self.assertEqual(toc, [0, 529200, 1323000])

    def test_read_unreadable_file_does_not_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.mp3"
            path.write_bytes(b"not audio at all")
            item = audiotags.read(path, cover=True)
            self.assertEqual(item.path, path)
            self.assertFalse(item.tagged)


@unittest.skipUnless(HAS_MUTAGEN, "mutagen not installed")
class MutagenTests(unittest.TestCase):
    def test_mp3_id3(self):
        item = audiotags.read(FIX / "tagged.mp3", cover=True)
        self.assertEqual((item.artist, item.album, item.title, item.year), ("Kispál és a Borz", "Ágy, asztal, tv", "Neked nem", "1994"))
        self.assertEqual((item.track, item.track_total, item.disc, item.disc_total), (1, 14, 1, 1))
        self.assertEqual(item.mb_album_id, "11111111-2222-3333-4444-555555555555")
        self.assertEqual(item.mb_release_group_id, "eb00dbf4-df9b-3435-92fd-4dedb251885e")
        self.assertEqual(item.mb_artist_id, "66666666-7777-8888-9999-000000000000")
        self.assertEqual(item.mb_recording_id, "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        self.assertEqual((item.barcode, item.catno, item.label, item.isrc), ("731452324942", "523 249-4", "PolyGram", "HUA019400001"))
        self.assertEqual(item.narrator, "Senki Sem")
        self.assertEqual(item.genres, ["Alternative Rock"])
        self.assertEqual((item.codec, item.bitrate_mode), ("mp3", "VBR"))
        self.assertTrue(item.cover.startswith(b"\x89PNG"))
        self.assertEqual(audiotags.read(FIX / "tagged.mp3").cover, b"")  # covers only on request

    def test_flac_vorbis(self):
        item = audiotags.read(FIX / "tagged.flac", cover=True)
        self.assertEqual((item.artist, item.album, item.title, item.date), ("Omega", "10000 lépés", "Gyöngyhajú lány", "1969-03-01"))
        self.assertEqual((item.track, item.track_total, item.disc, item.disc_total), (2, 10, 1, 2))
        self.assertEqual(item.genres, ["Rock", "Beat"])
        self.assertEqual((item.label, item.catno, item.discogs_release_id), ("Qualiton", "LPX 17400", "6577918"))
        self.assertEqual(item.mb_album_id, "89bd26a2-4d46-334f-91c2-5e8df803dab3")
        self.assertTrue(item.lossless)
        self.assertEqual((item.codec, item.bits_per_sample, item.sample_rate), ("flac", 16, 44100))
        self.assertEqual(item.total_samples, 22050)
        self.assertTrue(item.cover.startswith(b"\x89PNG"))
        self.assertEqual(audiotags.format_label([item]), "FLAC 16/44.1")

    def test_m4a_audiobook(self):
        item = audiotags.read(FIX / "audiobook.m4a", cover=True)
        self.assertEqual((item.artist, item.albumartist, item.album), ("Gárdonyi Géza", "Gárdonyi Géza", "Egri csillagok"))
        self.assertEqual(item.narrator, "Kaszás Attila")
        self.assertEqual(item.mb_album_id, "12345678-1234-1234-1234-123456789012")
        self.assertEqual((item.track, item.track_total), (1, 1))
        self.assertEqual(item.codec, "aac")
        self.assertEqual(item.genres, ["Audiobook"])
        self.assertTrue(item.cover.startswith(b"\x89PNG"))


class CueLogTests(unittest.TestCase):
    def test_disc_id_matches_musicbrainz(self):
        self.assertEqual(cuelog.disc_id(1, 9, DSOTM_LEADOUT, DSOTM_OFFSETS), DSOTM_DISCID)
        toc = cuelog.make_toc(1, 9, DSOTM_LEADOUT, DSOTM_OFFSETS)
        self.assertEqual(toc["toc"], "1+9+193293+183+18018+34143+66003+87533+116218+151488+166913+184200")

    def test_eac_log_utf16(self):
        log = cuelog.read_log(FIX / "eac_dsotm.log")
        self.assertEqual((log["tool"], log["artist"], log["album"]), ("EAC", "Pink Floyd", "Dark Side of the Moon"))
        self.assertEqual(len(log["tracks"]), 9)
        self.assertEqual(log["toc"]["discid"], DSOTM_DISCID)

    def test_xld_log_and_enhanced_cd(self):
        text = ("X Lossless Decoder version 20230627 (156.1)\n\nXLD extraction logfile from 2023-10-01 12:00:00 +0200\n\n"
                "AC/DC / Back in Black\n\nTOC of the extracted CD\n     Track |   Start  |  Length  | Start sector | End sector \n"
                "    ---------------------------------------------------------\n"
                "        1  | 00:00:00 | 00:13:26 |         0    |     1000   \n"
                "        2  | 00:13:26 | 00:13:25 |      1001    |     2000   \n"
                "        3  | 02:45:26 | 03:00:00 |     13401    |    26900   \n")
        log = cuelog.parse_log(text)
        self.assertEqual((log["tool"], log["artist"], log["album"]), ("XLD", "AC/DC", "Back in Black"))
        toc = log["toc"]  # track 3 is a data track 11400 frames after track 2
        self.assertEqual((toc["first"], toc["last"], toc["leadout"], toc["offsets"]), (1, 2, 13401 - 11400 + 150, [150, 1151]))

    def test_appended_logs_of_two_discs_use_the_first_toc(self):
        row = "     {n}  | 0:00.00 | 1:00.00 |  {s:>8}    | {e:>8}   \n"
        head = "Exact Audio Copy V1.6\n\nEAC extraction logfile from 1. January 2020\n\nArtist / Album\n\nTOC of the extracted CD\n"
        disc1 = head + "".join(row.format(n=i, s=(i - 1) * 1000, e=i * 1000 - 1) for i in range(1, 4))
        disc2 = head + "".join(row.format(n=i, s=(i - 1) * 2000, e=i * 2000 - 1) for i in range(1, 6))
        log = cuelog.parse_log(disc1 + "\n\n" + disc2)
        self.assertEqual([t[0] for t in log["tracks"]], [1, 2, 3])
        self.assertEqual(log["toc"]["toc"], "1+3+3150+150+1150+2150")

    def test_cue_cp1250_single_image(self):
        lines = ['REM GENRE "Alternative Rock"', "REM DATE 1994", "CATALOG 0731452324942", 'PERFORMER "Kispál és a Borz"',
                 'TITLE "Ágy, asztal, tv – Lőrinc és Ödön"', 'FILE "Kispal - Agy asztal tv.wav" WAVE']
        for number, offset in enumerate(DSOTM_OFFSETS, 1):
            lines += [f"  TRACK {number:02d} AUDIO", f'    TITLE "Szám {number} ű"', '    PERFORMER "Kispál és a Borz"']
            if number > 1:
                lines.append(f"    INDEX 00 {msf(offset - 150 - 30)}")
            lines.append(f"    INDEX 01 {msf(offset - 150)}")
        data = "\r\n".join(lines).encode("cp1250")
        cue = cuelog.parse_cue(cuelog.decode_text(data))
        self.assertEqual(cue["title"], "Ágy, asztal, tv – Lőrinc és Ödön")
        self.assertEqual((cue["genre"], cue["date"], cue["catalog"]), ("Alternative Rock", "1994", "0731452324942"))
        self.assertEqual(cue["files"], ["Kispal - Agy asztal tv.wav"])
        self.assertEqual(len(cue["tracks"]), 9)
        self.assertEqual(cue["tracks"][1]["title"], "Szám 2 ű")
        self.assertEqual(cue["tracks"][1]["index1"], DSOTM_OFFSETS[1] - 150)
        frames = {"Kispal - Agy asztal tv.wav": DSOTM_LEADOUT - 150}
        self.assertEqual(cuelog.toc_from_cue(cue, frames)["discid"], DSOTM_DISCID)
        lengths = cuelog.cue_lengths(cue, frames)
        self.assertAlmostEqual(lengths[1], (DSOTM_OFFSETS[1] - DSOTM_OFFSETS[0]) / 75)
        self.assertAlmostEqual(lengths[9], (DSOTM_LEADOUT - DSOTM_OFFSETS[8]) / 75)
        self.assertEqual(cuelog.toc_from_cue(cue, {}), {})
        self.assertEqual(cuelog.match_file("Kispal - Agy asztal tv.wav", [Path("/x/kispal - agy asztal tv.flac"), Path("/x/other.flac")]).name,
                         "kispal - agy asztal tv.flac")

    def test_cue_gaps_appended_multi_file(self):
        text = ('FILE "01.wav" WAVE\n  TRACK 01 AUDIO\n    TITLE "A"\n    INDEX 01 00:00:00\n  TRACK 02 AUDIO\n    TITLE "B"\n'
                '    INDEX 00 00:58:00\nFILE "02.wav" WAVE\n    INDEX 01 00:00:00\n  TRACK 03 AUDIO\n    TITLE "C"\n    INDEX 01 00:30:00\n')
        cue = cuelog.parse_cue(text)
        self.assertEqual([t["file"] for t in cue["tracks"]], ["01.wav", "02.wav", "02.wav"])
        toc = cuelog.toc_from_cue(cue, {"01.wav": 60 * 75, "02.wav": 100 * 75})
        self.assertEqual(toc["offsets"], [150, 150 + 60 * 75, 150 + 90 * 75])
        self.assertEqual(toc["leadout"], 150 + 160 * 75)

    def test_decode_text(self):
        self.assertEqual(cuelog.decode_text("Árvíztűrő".encode("cp1250")), "Árvíztűrő")
        self.assertEqual(cuelog.decode_text("Café à la carte".encode("cp1252")), "Café à la carte")
        self.assertEqual(cuelog.decode_text("﻿őű".encode("utf-8")), "őű")
        self.assertEqual(cuelog.decode_text("abc".encode("utf-16")), "abc")

    def test_flac_cuesheet_and_lengths(self):
        starts = [(o - 150) * 588 for o in DSOTM_OFFSETS] + [(DSOTM_LEADOUT - 150) * 588]
        self.assertEqual(cuelog.toc_from_flac_cuesheet(starts)["discid"], DSOTM_DISCID)
        toc = cuelog.toc_from_lengths([588 * 75 * 60, 588 * 75 * 30])
        self.assertEqual((toc["offsets"], toc["leadout"]), ([150, 150 + 4500], 150 + 6750))
        self.assertEqual(cuelog.toc_from_lengths([]), {})


def mpls(items: list[tuple[int, int]], marks: list[tuple[int, int, int]]) -> bytes:
    """A minimal Blu-ray playlist: PlayItems (IN, OUT) and marks (type, item, time) in 45 kHz ticks."""

    body = b""
    for in_time, out_time in items:
        item = b"00001" + b"M2TS" + b"\x00\x00" + b"\x00" + struct.pack(">II", in_time, out_time) + b"\x00" * 8
        body += struct.pack(">H", len(item)) + item
    playlist = struct.pack(">IHHH", 0, 0, len(items), 0) + body
    mark_data = struct.pack(">IH", 0, len(marks)) + b"".join(struct.pack(">BBHIHI", 0, t, i, ts, 0x1011, 0) for t, i, ts in marks)
    header_len = 40
    return b"MPLS0200" + struct.pack(">III", header_len, header_len + len(playlist), 0) + b"\x00" * (header_len - 20) + playlist + mark_data


class ChaptersTests(unittest.TestCase):
    def test_ffprobe_chapters(self):
        named = chapters.parse_chapters(json.loads((FIX / "ffprobe_chapters_m4b.json").read_text(encoding="utf-8")))
        self.assertEqual(len(named), 5)
        self.assertEqual(named[1]["title"], "2. rész: Oda Buda!")
        self.assertEqual((named[1]["start"], named[1]["end"]), (60.0, 120.0))
        self.assertEqual(chapters.named(named), named)
        generic = chapters.parse_chapters(json.loads((FIX / "ffprobe_chapters_placeholder.json").read_text(encoding="utf-8")))
        self.assertTrue(generic and all(c["placeholder"] for c in generic))
        self.assertEqual(chapters.named(generic), [])

    def test_placeholders(self):
        for title in ("Chapter 01", "Fejezet 1", "1. fejezet", "01", "00:05:00.000", "Track 5", "", "Kapitel 12", "Rész 3"):
            self.assertTrue(chapters.is_placeholder(title), title)
        for title in ("Money", "1984", "Shine On You Crazy Diamond", "1. rész: Hol terem a magyar vitéz?"):
            self.assertFalse(chapters.is_placeholder(title), title)
        self.assertEqual((chapters.length_text(61), chapters.length_text(3725), chapters.length_text(0)), ("1:01", "1:02:05", ""))

    def test_mpls(self):
        tick = 45000
        data = mpls([(tick * 10, tick * 70), (tick * 5, tick * 65)], [(1, 0, tick * 10), (1, 0, tick * 40), (1, 1, tick * 5), (2, 1, tick * 6), (1, 1, tick * 63)])
        info = chapters.parse_mpls(data)
        self.assertEqual(info["duration"], 120)
        self.assertEqual(info["chapters"], [0.0, 30.0, 60.0])  # the mark 2 s before the end and the non-entry mark are dropped
        self.assertEqual(chapters.parse_mpls(b"nope"), {})

    def test_lsdvd_xml(self):
        text = ("<lsdvd><track><ix>1</ix><length>12.5</length><chapter><ix>1</ix><length>12.5</length></chapter></track>"
                "<track><ix>2</ix><length>300.0</length><chapter><ix>1</ix><length>100.0</length></chapter>"
                "<chapter><ix>2</ix><length>200.0</length></chapter></track></lsdvd>")
        titles = chapters.parse_lsdvd_xml(text)
        self.assertEqual([t["title"] for t in titles], [1, 2])
        self.assertEqual(titles[1]["chapters"], [100.0, 200.0])


class FingerprintTests(unittest.TestCase):
    def test_parse_fpcalc(self):
        result = fingerprint.parse_fpcalc((FIX / "fpcalc.json").read_text(encoding="utf-8"))
        self.assertEqual(result["duration"], 201)
        self.assertTrue(result["fingerprint"].startswith("AQAA"))
        self.assertEqual(fingerprint.parse_fpcalc("garbage"), {})
        self.assertEqual(fingerprint.parse_fpcalc('{"duration": 0, "fingerprint": "AQ"}'), {})
        self.assertEqual(fingerprint.parse_fpcalc("[1, 2]"), {})


if __name__ == "__main__":
    unittest.main()
