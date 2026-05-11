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
