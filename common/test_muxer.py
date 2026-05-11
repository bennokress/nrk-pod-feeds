import subprocess
from urllib.parse import urlparse

import requests

from . import muxer
from . import tvapi


_SYNTHETIC_MASTER = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-INDEPENDENT-SEGMENTS

# Variants
#EXT-X-STREAM-INF:BANDWIDTH=647215,AVERAGE-BANDWIDTH=646923,CODECS="avc1.4d401e,mp4a.40.2",RESOLUTION=640x360,FRAME-RATE=25.000
sc-gaFEAA/m10_index.m3u8?adap=small
#EXT-X-STREAM-INF:BANDWIDTH=2205368,AVERAGE-BANDWIDTH=2197454,CODECS="avc1.64001f,mp4a.40.2",RESOLUTION=1280x720,FRAME-RATE=25.000
sc-gaFEAA/m31_index.m3u8?adap=small
#EXT-X-STREAM-INF:BANDWIDTH=1309459,AVERAGE-BANDWIDTH=1303468,CODECS="avc1.4d401f,mp4a.40.2",RESOLUTION=960x540,FRAME-RATE=25.000
sc-gaFEAA/m21_index.m3u8?adap=small
"""


def test_pick_best_variant_prefers_highest_bandwidth():
    master_url = "https://nrk-od-world-58.akamaized.net/open/ps/foo/bar.smil/muxed.m3u8?adap=small"
    best = muxer.pick_best_variant(master_url, playlist_text=_SYNTHETIC_MASTER)
    # 720p variant (m31) at 2.2 Mbps wins over 540p (1.3 Mbps) and 360p (647 kbps)
    assert "m31_index.m3u8" in best
    # Returned URL is absolute and resolves against the master URL base
    assert best.startswith("https://nrk-od-world-58.akamaized.net/open/ps/foo/bar.smil/")


def test_pick_best_variant_returns_master_when_no_variants():
    media_playlist = "#EXTM3U\n#EXT-X-VERSION:3\n#EXTINF:6.0,\nsegment0.ts\n"
    url = "https://example.com/segment-playlist.m3u8"
    assert muxer.pick_best_variant(url, playlist_text=media_playlist) == url


def test_write_ffmetadata_produces_expected_format(tmp_path):
    chapters = [
        {"title": "Intro", "start_seconds": 0},
        {"title": "Krigen i Ukraina", "start_seconds": 27},
        {"title": "Værmelding", "start_seconds": 480},
    ]
    out = tmp_path / "chapters.ffmetadata"
    muxer._write_ffmetadata(str(out), chapters, total_duration_seconds=520)

    content = out.read_text(encoding="utf-8")
    assert content.startswith(";FFMETADATA1\n")
    assert content.count("[CHAPTER]") == 3
    assert content.count("TIMEBASE=1/1000") == 3
    # First chapter spans to the next chapter's start
    assert "[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=27000\ntitle=Intro" in content
    assert "[CHAPTER]\nTIMEBASE=1/1000\nSTART=27000\nEND=480000\ntitle=Krigen i Ukraina" in content
    # Last chapter ends at total duration
    assert "[CHAPTER]\nTIMEBASE=1/1000\nSTART=480000\nEND=520000\ntitle=Værmelding" in content


def test_write_ffmetadata_skips_chapters_with_empty_titles(tmp_path):
    chapters = [
        {"title": "Real chapter", "start_seconds": 0},
        {"title": "", "start_seconds": 10},
        {"title": "Another real chapter", "start_seconds": 20},
    ]
    out = tmp_path / "chapters.ffmetadata"
    muxer._write_ffmetadata(str(out), chapters, total_duration_seconds=30)

    content = out.read_text(encoding="utf-8")
    assert content.count("[CHAPTER]") == 2
    assert "Real chapter" in content
    assert "Another real chapter" in content


class _FakeCompletedProcess:
    def __init__(self):
        self.returncode = 1
        self.stderr = "test stub"
        self.stdout = ""


def test_mux_to_mp4_argv_includes_metadata_input_when_chapters_provided(monkeypatch, tmp_path):
    captured = {}

    def fake_run(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        return _FakeCompletedProcess()

    monkeypatch.setattr(subprocess, "run", fake_run)

    chapters = [
        {"title": "A", "start_seconds": 0},
        {"title": "B", "start_seconds": 10},
    ]
    out = tmp_path / "out.mp4"

    try:
        muxer.mux_to_mp4(
            "https://example.test/master.m3u8", str(out),
            chapters=chapters, total_duration_seconds=30,
        )
    except RuntimeError:
        pass  # expected — stubbed ffmpeg returns rc=1

    cmd = captured["cmd"]
    assert cmd.count("-i") == 2, f"expected HLS + metadata inputs, got: {cmd}"
    assert "-map" in cmd
    assert cmd[cmd.index("-map") + 1] == "0"
    assert "-map_metadata" in cmd
    assert cmd[cmd.index("-map_metadata") + 1] == "1"
    # Metadata file is cleaned up in the finally block even on ffmpeg failure
    assert not (tmp_path / "out.mp4.ffmetadata").exists()


def test_mux_to_mp4_argv_omits_metadata_input_when_no_chapters(monkeypatch, tmp_path):
    captured = {}

    def fake_run(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        return _FakeCompletedProcess()

    monkeypatch.setattr(subprocess, "run", fake_run)

    out = tmp_path / "out.mp4"
    try:
        muxer.mux_to_mp4("https://example.test/master.m3u8", str(out))
    except RuntimeError:
        pass

    cmd = captured["cmd"]
    assert cmd.count("-i") == 1
    assert "-map_metadata" not in cmd


def test_pick_best_variant_against_real_nrk_master():
    """Live integration: walk to the international Dagsrevyen and parse a real master playlist."""
    instalments = tvapi.get_latest_instalments(
        "dagsrevyen-for-utlandet", limit=1, playable_only=True
    )
    assert instalments

    program_id = instalments[0]["prfId"]
    manifest = tvapi.get_program_manifest(program_id)
    assert manifest

    hls_url, _ = tvapi.get_hls_stream_url(manifest)
    assert hls_url

    best = muxer.pick_best_variant(hls_url)
    # The variant URL itself should be reachable and contain .m3u8
    parsed = urlparse(best)
    assert parsed.scheme in ("http", "https")
    assert ".m3u8" in parsed.path

    head = requests.get(best, headers={"User-Agent": "nrk-pod-feeder-test"}, timeout=15)
    assert head.status_code == 200
