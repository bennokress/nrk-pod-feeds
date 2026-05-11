import logging
import os
import re
import subprocess
from urllib.parse import urljoin

import requests

from common.helpers import get_version


_STREAM_INF_RE = re.compile(r"#EXT-X-STREAM-INF:(?P<attrs>.+)")
_BANDWIDTH_RE = re.compile(r"BANDWIDTH=(\d+)")
_USER_AGENT = f"nrk-pod-feeder {get_version()}"


def _parse_variants(playlist_text):
    """
    Parse an HLS master playlist and yield (bandwidth, variant_path) tuples.
    The variant_path is the line that immediately follows each #EXT-X-STREAM-INF.
    """
    lines = [l.strip() for l in playlist_text.splitlines()]
    for i, line in enumerate(lines):
        m = _STREAM_INF_RE.match(line)
        if not m:
            continue
        bw_match = _BANDWIDTH_RE.search(m.group("attrs"))
        if not bw_match:
            continue
        # The variant URI is the next non-blank, non-comment line.
        for j in range(i + 1, len(lines)):
            candidate = lines[j]
            if candidate and not candidate.startswith("#"):
                yield int(bw_match.group(1)), candidate
                break


def pick_best_variant(master_m3u8_url, playlist_text=None):
    """
    Fetch the master playlist (if not supplied) and return the highest-bandwidth
    variant playlist URL, resolved against the master URL.
    Returns the master URL itself if no #EXT-X-STREAM-INF entries are present
    (i.e. the URL is already a media playlist).
    """
    if playlist_text is None:
        r = requests.get(master_m3u8_url, headers={"User-Agent": _USER_AGENT}, timeout=30)
        r.raise_for_status()
        playlist_text = r.text

    variants = list(_parse_variants(playlist_text))
    if not variants:
        return master_m3u8_url

    _, best_path = max(variants, key=lambda v: v[0])
    return urljoin(master_m3u8_url, best_path)


def mux_to_mp4(variant_m3u8_url, output_path):
    """
    Mux the given HLS media playlist into a single progressive MP4 using ffmpeg
    with stream copy (no re-encode). Returns the local file size in bytes.

    Flags:
      -c copy                       no re-encode
      -bsf:a aac_adtstoasc          fix AAC bitstream for the MP4 container
      -movflags +faststart          place moov atom at start for early playback
    """
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "warning",
        "-y",
        "-user_agent", _USER_AGENT,
        "-i", variant_m3u8_url,
        "-c", "copy",
        "-bsf:a", "aac_adtstoasc",
        "-movflags", "+faststart",
        output_path,
    ]
    logging.info(f"  Muxing HLS to {output_path}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        logging.warning(f"ffmpeg failed (rc={result.returncode}): {result.stderr[:500]}")
        if os.path.exists(output_path):
            os.remove(output_path)
        raise RuntimeError(f"ffmpeg mux failed for {variant_m3u8_url}")

    size = os.path.getsize(output_path)
    logging.info(f"  Muxed MP4 size: {size:,} bytes")
    return size
