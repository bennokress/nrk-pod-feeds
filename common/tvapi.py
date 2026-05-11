import logging
import re
import requests

from dateutil import parser as _date_parser

from common.helpers import get_version

api_base_url = "https://psapi.nrk.no"
headers = {
    "User-Agent": f"nrk-pod-feeder {get_version()}"
}

# Cache of {series_id: {date: instalment}} populated lazily by
# find_instalment_by_release_date(). Per-process only; resets every run.
_instalment_index_cache = {}


def parse_iso_duration(iso_duration):
    """
    Parse ISO 8601 duration to seconds.
    Example: "PT43M33.76S" -> 2613
    """
    if not iso_duration:
        return 0

    pattern = r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?'
    match = re.match(pattern, iso_duration)
    if not match:
        return 0

    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = float(match.group(3) or 0)

    return int(hours * 3600 + minutes * 60 + seconds)


def get_series_metadata(series_id, format="json"):
    """
    Fetch TV series metadata.
    Endpoint: GET /tv/catalog/series/{series_id}
    """
    logging.debug(f"Fetching metadata for TV series {series_id}...")

    url = f"{api_base_url}/tv/catalog/series/{series_id}"
    r = requests.get(url, headers=headers)

    if not r.ok:
        logging.info(f"Unable to fetch TV series metadata ({url} returned {r.status_code})")
        return None

    if format == "text":
        return r.text

    return r.json()


def get_series_seasons(series_id):
    """
    Get list of available seasons/months for a TV series, newest first.
    Returns list of season objects with 'id' and 'title'.
    """
    metadata = get_series_metadata(series_id)
    if not metadata:
        return None

    # _embedded.seasons holds the full archive (oldest-first); navigation.subnavigation
    # only exposes a curated handful of historical months and is missing the current ones.
    embedded_seasons = metadata.get("_embedded", {}).get("seasons", []) or []

    seasons = []
    for season in embedded_seasons:
        if season.get("hasAvailableInstalments") is False:
            continue
        self_link = season.get("_links", {}).get("self", {})
        season_id = self_link.get("name")
        if not season_id:
            continue
        seasons.append({
            "id": season_id,
            "title": self_link.get("title") or season.get("titles", {}).get("title")
        })

    seasons.reverse()
    return seasons


def get_series_instalments(series_id, month_id, format="json"):
    """
    Fetch episodes (instalments) for a TV series season/month.
    Endpoint: GET /tv/catalog/series/{series_id}/seasons/{month_id}
    """
    logging.info(f"Fetching instalments for TV series {series_id} ({month_id})...")

    url = f"{api_base_url}/tv/catalog/series/{series_id}/seasons/{month_id}"
    r = requests.get(url, headers=headers)

    if not r.ok:
        logging.info(f"Unable to fetch TV series instalments ({url} returned {r.status_code})")
        return None

    if format == "text":
        return r.text

    data = r.json()
    return data.get("_embedded", {}).get("instalments", [])


def iter_latest_instalments(series_id, max_months=12, playable_only=True):
    """
    Generator that yields instalments from most recent to oldest.
    Iterates through seasons/months up to max_months.

    Args:
        series_id: The TV series identifier
        max_months: Maximum number of months to check (default 12)
        playable_only: If True, skip episodes with status "coming"

    Yields:
        Instalment dicts one at a time
    """
    seasons = get_series_seasons(series_id)
    if not seasons:
        logging.info(f"No seasons found for TV series {series_id}")
        return

    months_checked = 0

    for season in seasons:
        if months_checked >= max_months:
            logging.debug(f"  Reached max_months limit ({max_months})")
            break

        month_id = season["id"]
        month_instalments = get_series_instalments(series_id, month_id)
        months_checked += 1

        if month_instalments:
            for inst in month_instalments:
                # Skip episodes that are not yet playable
                if playable_only:
                    availability = inst.get("availability", {})
                    status = availability.get("status", "")
                    if status == "coming":
                        logging.debug(f"  Skipping upcoming episode: {inst.get('prfId')}")
                        continue

                yield inst


def get_latest_instalments(series_id, limit=10, playable_only=True):
    """
    Get the most recent instalments across seasons.
    Fetches from most recent month first, continues to older months if needed.

    Args:
        series_id: The TV series identifier
        limit: Maximum number of instalments to return
        playable_only: If True, filter out episodes that are not yet available
    """
    logging.info(f"Fetching latest {limit} instalments for TV series {series_id}...")

    instalments = []
    for inst in iter_latest_instalments(series_id, playable_only=playable_only):
        instalments.append(inst)
        if len(instalments) >= limit:
            break

    return instalments if instalments else None


def is_geo_blocked(instalment):
    """True if the instalment is geo-restricted (e.g. region "Norge")."""
    return bool(
        (instalment or {})
        .get("usageRights", {})
        .get("geoBlock", {})
        .get("isGeoBlocked")
    )


def find_instalment_by_release_date(series_id, target_date, scan_limit=30):
    """
    Find an instalment of `series_id` whose releaseDateOnDemand falls on
    `target_date` (a datetime.date). Returns the instalment dict or None.

    Builds a lazy {date: instalment} index per series_id on first call so
    repeated lookups within the same run share one season-walk.
    """
    index = _instalment_index_cache.get(series_id)
    if index is None:
        index = {}
        scanned = 0
        for inst in iter_latest_instalments(series_id, playable_only=True):
            release = inst.get("releaseDateOnDemand")
            if release:
                try:
                    inst_date = _date_parser.parse(release).date()
                except (ValueError, TypeError):
                    continue
                index.setdefault(inst_date, inst)
            scanned += 1
            if scanned >= scan_limit:
                break
        _instalment_index_cache[series_id] = index
    return index.get(target_date)


def get_program_manifest(program_id, format="json"):
    """
    Fetch playback manifest for a program.
    Endpoint: GET /playback/manifest/program/{program_id}
    """
    logging.debug(f"  Fetching manifest for program {program_id}...")

    url = f"{api_base_url}/playback/manifest/program/{program_id}"
    r = requests.get(url, headers=headers)

    if not r.ok:
        logging.info(f"  Unable to fetch program manifest ({url} returned {r.status_code})")
        return None

    if format == "text":
        return r.text

    return r.json()


def get_subtitles(manifest, types=("nor", "ttv")):
    """
    Extract the subtitle tracks from a playback manifest.

    Returns a list of dicts with keys: type, language, label, webVtt,
    default_on. Entries are filtered to the requested `types` and returned in
    the order of `types` (so callers can present the Forced track before the
    Full SDH track, for example).

    NRK exposes two Norwegian WebVTT tracks:
      type="nor" -> "Forced" (translations of non-Norwegian speech only)
      type="ttv" -> "Full SDH" (every speaker, including Norwegian)
    The label "Norsk" on the Forced track is intentionally ambiguous on NRK's
    side; the filename suffix (non-sdh-translated vs sdh) is the authoritative
    cue if you need to disambiguate.

    Entries without a webVtt URL are skipped.
    """
    if not manifest:
        return []

    playable = manifest.get("playable")
    if not playable:
        return []

    raw = playable.get("subtitles") or []
    by_type = {}
    for entry in raw:
        vtt = entry.get("webVtt")
        if not vtt:
            continue
        by_type[entry.get("type")] = {
            "type": entry.get("type"),
            "language": entry.get("language"),
            "label": entry.get("label"),
            "webVtt": vtt,
            "default_on": bool(entry.get("defaultOn")),
        }

    return [by_type[t] for t in types if t in by_type]


def get_hls_stream_url(manifest):
    """
    Extract HLS stream URL and MIME type from manifest.
    Returns: (url, mime_type) or None if not found.
    """
    if not manifest:
        return None

    playable = manifest.get("playable")
    if not playable:
        return None

    assets = playable.get("assets", [])

    for asset in assets:
        if asset.get("format") == "HLS":
            return (asset.get("url"), asset.get("mimeType"))

    # If no HLS found, return first asset if available
    if assets:
        return (assets[0].get("url"), assets[0].get("mimeType"))

    return None


def get_series_title(series_id):
    """
    Get the title of a TV series.
    """
    metadata = get_series_metadata(series_id)
    if not metadata:
        return None

    # Try different paths where title might be
    news = metadata.get("news", {})
    titles = news.get("titles", {})
    title = titles.get("title")

    if title:
        return title

    # Fallback to series ID formatted
    return series_id.replace("-", " ").title()


def get_series_image(series_id):
    """
    Get the image URL for a TV series.
    """
    metadata = get_series_metadata(series_id)
    if not metadata:
        return None

    news = metadata.get("news", {})
    images = news.get("image", [])

    # Get highest resolution image (last in array usually)
    if images and len(images) > 0:
        # Try to get a larger image
        for img in reversed(images):
            if img.get("url"):
                return img.get("url")

    return None


def get_program_playback_metadata(program_id, format="json"):
    """
    Fetch playback metadata for a program including index points (chapters).
    Endpoint: GET /playback/metadata/program/{program_id}
    """
    logging.debug(f"  Fetching playback metadata for program {program_id}...")

    url = f"{api_base_url}/playback/metadata/program/{program_id}"
    r = requests.get(url, headers=headers)

    if not r.ok:
        logging.debug(f"  Unable to fetch playback metadata ({url} returned {r.status_code})")
        return None

    if format == "text":
        return r.text

    return r.json()


def get_index_points(program_id):
    """
    Extract index points (chapters) from program playback metadata.
    Returns list of dicts with: title, start_seconds, image_url (optional)
    Returns empty list if no index points available.
    """
    metadata = get_program_playback_metadata(program_id)
    if not metadata:
        return []

    preplay = metadata.get("preplay", {})
    index_points = preplay.get("indexPoints", [])

    if not index_points:
        return []

    chapters = []
    for point in index_points:
        title = point.get("title", "")
        if not title:
            continue

        start_point = point.get("startPoint", "PT0S")
        start_seconds = parse_iso_duration(start_point)

        # Get thumbnail if available (prefer medium size around 640px)
        thumbnails = point.get("thumbnails", [])
        image_url = None
        if thumbnails:
            # Try to find a medium-sized thumbnail
            for thumb in thumbnails:
                if thumb.get("pixelWidth", 0) >= 480:
                    image_url = thumb.get("url")
                    break
            # Fallback to first thumbnail
            if not image_url and thumbnails:
                image_url = thumbnails[0].get("url")

        chapters.append({
            "title": title,
            "start_seconds": start_seconds,
            "image_url": image_url
        })

    logging.debug(f"  Found {len(chapters)} chapters for program {program_id}")
    return chapters
