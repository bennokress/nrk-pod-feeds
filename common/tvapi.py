import logging
import re
import requests

from common.helpers import get_version

api_base_url = "https://psapi.nrk.no"
headers = {
    "User-Agent": f"nrk-pod-feeder {get_version()}"
}


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
    Get list of available seasons/months for a TV series.
    Returns list of season objects with 'id' and 'title'.
    """
    metadata = get_series_metadata(series_id)
    if not metadata:
        return None

    seasons = []
    nav = metadata.get("navigation", {})

    # Find the subnavigation section containing seasons
    for section in nav.get("sections", []):
        if section.get("type") == "subnavigation":
            for subsection in section.get("sections", []):
                if subsection.get("type") == "season":
                    seasons.append({
                        "id": subsection.get("id"),
                        "title": subsection.get("title")
                    })

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

    seasons = get_series_seasons(series_id)
    if not seasons:
        logging.info(f"No seasons found for TV series {series_id}")
        return None

    instalments = []

    for season in seasons:
        if len(instalments) >= limit:
            break

        month_id = season["id"]
        month_instalments = get_series_instalments(series_id, month_id)

        if month_instalments:
            for inst in month_instalments:
                if len(instalments) >= limit:
                    break

                # Skip episodes that are not yet playable
                if playable_only:
                    availability = inst.get("availability", {})
                    status = availability.get("status", "")
                    if status == "coming":
                        logging.debug(f"  Skipping upcoming episode: {inst.get('prfId')}")
                        continue

                instalments.append(inst)

    return instalments


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
