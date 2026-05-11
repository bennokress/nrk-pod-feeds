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


def _write_ffmetadata(path, chapters, total_duration_seconds):
    """Write an ffmpeg metadata file with chapter entries.

    Format reference: https://ffmpeg.org/ffmpeg-formats.html#Metadata-1

    Each chapter contributes one `[CHAPTER]` block with millisecond-resolution
    START / END times. END is derived from the next chapter's START, or the
    supplied total duration for the last chapter (with a 1-ms minimum span as
    a defensive floor).
    """
    total_ms = int((total_duration_seconds or 0) * 1000)
    starts_ms = [int(c.get("start_seconds", 0) * 1000) for c in chapters]

    with open(path, "w", encoding="utf-8") as f:
        f.write(";FFMETADATA1\n")
        for i, chapter in enumerate(chapters):
            title = (chapter.get("title") or "").strip()
            if not title:
                continue
            start = starts_ms[i]
            if i + 1 < len(starts_ms):
                end = starts_ms[i + 1]
            elif total_ms > start:
                end = total_ms
            else:
                end = start + 1
            f.write("\n[CHAPTER]\n")
            f.write("TIMEBASE=1/1000\n")
            f.write(f"START={start}\n")
            f.write(f"END={end}\n")
            f.write(f"title={title}\n")


def mux_to_mp4(variant_m3u8_url, output_path, chapters=None, total_duration_seconds=None):
    """
    Mux the given HLS media playlist into a single progressive MP4 using ffmpeg
    with stream copy (no re-encode). Returns the local file size in bytes.

    When `chapters` is a non-empty list, an ffmpeg metadata file is generated
    from the chapter data and merged into the output as native MP4 chapter
    atoms (`moov.udta.chpl`), which Apple Podcasts surfaces as a tappable
    chapter list. `total_duration_seconds` is used to derive the END time of
    the last chapter; pass the episode duration from the NRK manifest.

    Flags:
      -c copy                       no re-encode
      -bsf:a aac_adtstoasc          fix AAC bitstream for the MP4 container
      -movflags +faststart          place moov atom at start for early playback
      -map 0 -map_metadata 1        (chapter mode) preserve HLS streams, take
                                    metadata from the chapters input
    """
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "warning",
        "-y",
        "-user_agent", _USER_AGENT,
        "-i", variant_m3u8_url,
    ]

    metadata_path = None
    if chapters:
        metadata_path = output_path + ".ffmetadata"
        _write_ffmetadata(metadata_path, chapters, total_duration_seconds)
        cmd += ["-i", metadata_path, "-map", "0", "-map_metadata", "1"]

    cmd += [
        "-c", "copy",
        "-bsf:a", "aac_adtstoasc",
        "-movflags", "+faststart",
        output_path,
    ]

    logging.info(f"  Muxing HLS to {output_path}")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    finally:
        if metadata_path and os.path.exists(metadata_path):
            os.remove(metadata_path)

    if result.returncode != 0:
        logging.warning(f"ffmpeg failed (rc={result.returncode}): {result.stderr[:500]}")
        if os.path.exists(output_path):
            os.remove(output_path)
        raise RuntimeError(f"ffmpeg mux failed for {variant_m3u8_url}")

    size = os.path.getsize(output_path)
    logging.info(f"  Muxed MP4 size: {size:,} bytes")
    return size
