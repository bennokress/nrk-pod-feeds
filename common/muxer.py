import logging
import os
import re
import subprocess
import tempfile
from urllib.parse import urljoin

import requests

from common.helpers import get_version


_STREAM_INF_RE = re.compile(r"#EXT-X-STREAM-INF:(?P<attrs>.+)")
_BANDWIDTH_RE = re.compile(r"BANDWIDTH=(\d+)")
_USER_AGENT = f"nrk-pod-feeder {get_version()}"

# NRK exposes ISO 639-1 codes (e.g. "nb"); MP4 timed-text expects ISO 639-2
# 3-letter codes. Unknown codes are passed through unchanged.
_LANG_ISO639_1_TO_2 = {
    "nb": "nob",
    "nn": "nno",
    "no": "nor",
    "en": "eng",
    "se": "sme",
}


def _language_to_iso639_2(code):
    if not code:
        return "und"
    return _LANG_ISO639_1_TO_2.get(code, code)


def _download_subtitles_to_dir(subtitles, target_dir):
    """Fetch each subtitle's WebVTT URL to a local file under `target_dir`.

    Returns the list of `{path, language, title}` dicts in the same order as
    the input, skipping any whose fetch failed (logged at WARNING). Never
    raises — a partial or empty result is preferable to failing the mux.
    """
    out = []
    for sub in subtitles:
        vtt_url = sub.get("webVtt")
        if not vtt_url:
            continue
        sub_type = sub.get("type") or "sub"
        local_path = os.path.join(target_dir, f"{sub_type}.vtt")
        try:
            r = requests.get(
                vtt_url, headers={"User-Agent": _USER_AGENT}, timeout=30,
            )
            r.raise_for_status()
            with open(local_path, "wb") as f:
                f.write(r.content)
        except Exception as e:
            logging.warning(f"  subtitle download failed ({sub_type}): {e}")
            continue
        out.append({
            "path": local_path,
            "language": _language_to_iso639_2(sub.get("language")),
            "title": sub.get("label") or "",
        })
    return out


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


def mux_to_mp4(
    variant_m3u8_url, output_path,
    chapters=None, total_duration_seconds=None, subtitles=None,
):
    """
    Mux the given HLS media playlist into a single progressive MP4 using ffmpeg
    with stream copy (no re-encode). Returns the local file size in bytes.

    Optional inputs:

    - `chapters` (list of {title, start_seconds, ...} from `get_index_points`)
      + `total_duration_seconds`: when present, an ffmpeg metadata file is
      generated and merged via `-map_metadata`, baking native MP4 chapter
      atoms (`moov.udta.chpl`) that Apple Podcasts surfaces as a tappable
      chapter list.

    - `subtitles` (list of {webVtt, language, label, ...} from
      `tvapi.get_subtitles`): each WebVTT is downloaded to a temporary file
      and added as a separate ffmpeg input. Output MP4 carries one timed-text
      track per subtitle (`mov_text` codec), with `language=<iso639-2>` and
      `title=<NRK label>` metadata so the captions menu in Apple Podcasts
      labels them correctly.

    Flags (ordered after inputs and maps):
      -c copy                       no re-encode for video/audio
      -c:s mov_text                 (subtitle mode) convert WebVTT to MP4 timed text
      -bsf:a aac_adtstoasc          fix AAC bitstream for the MP4 container
      -movflags +faststart          place moov atom at start for early playback
    """
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="nrk-mux-") as workdir:
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "warning",
            "-y",
            "-user_agent", _USER_AGENT,
            "-i", variant_m3u8_url,
        ]

        # Subtitle inputs: 1..N
        downloaded_subs = []
        if subtitles:
            downloaded_subs = _download_subtitles_to_dir(subtitles, workdir)
            for sub in downloaded_subs:
                cmd += ["-i", sub["path"]]

        # Chapter metadata input: last
        if chapters:
            metadata_path = os.path.join(workdir, "chapters.ffmetadata")
            _write_ffmetadata(metadata_path, chapters, total_duration_seconds)
            cmd += ["-i", metadata_path]

        # Stream mapping
        if downloaded_subs:
            cmd += ["-map", "0:v", "-map", "0:a"]
            for i, _ in enumerate(downloaded_subs):
                cmd += ["-map", str(i + 1)]
        elif chapters:
            cmd += ["-map", "0"]

        if chapters:
            metadata_input_index = 1 + len(downloaded_subs)
            cmd += ["-map_metadata", str(metadata_input_index)]

        # Codecs
        cmd += ["-c", "copy", "-bsf:a", "aac_adtstoasc"]
        if downloaded_subs:
            cmd += ["-c:s", "mov_text"]
            for i, sub in enumerate(downloaded_subs):
                cmd += [f"-metadata:s:s:{i}", f"language={sub['language']}"]
                if sub.get("title"):
                    cmd += [f"-metadata:s:s:{i}", f"title={sub['title']}"]

        cmd += ["-movflags", "+faststart", output_path]

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
