import json
import logging
import os
import tempfile
import uuid
import xml.etree.ElementTree as ET

import requests
from podgen import Podcast, Episode, Media
from dateutil import parser
from datetime import timedelta

from common.helpers import init, get_last_feed, get_podcasts_config, get_version
from common.tvapi import (
    get_series_metadata,
    get_series_title,
    get_series_image,
    get_latest_instalments,
    iter_latest_instalments,
    get_program_manifest,
    get_hls_stream_url,
    get_subtitles,
    parse_iso_duration,
    get_index_points,
    is_geo_blocked,
    find_instalment_by_release_date
)
from common.bunny import BunnyStorage
from common.muxer import pick_best_variant, mux_to_mp4

podgen_agent = f"nrk-pod-feeder v{get_version()} (with help from python-podgen)"
tv_programs_cfg_file = "tv_programs.json"
web_url = "https://bennokress.github.io/nrk-pod-feeds"

# HLS video MIME type
VIDEO_MIME_TYPE = "application/vnd.apple.mpegurl"

# Track chapters for each episode (keyed by video URL)
episode_chapters = {}

# Podcasting 2.0 namespace (for medium tag)
PODCAST_NS = 'https://podcastindex.org/namespace/1.0'

# Podcasting 2.0 JSON chapters
JSDELIVR_BASE = "https://cdn.jsdelivr.net/gh/bennokress/nrk-pod-feeds@main/docs/chapters"
CHAPTERS_DIR = "docs/chapters"

# Track episode metadata for JSON chapters (keyed by video URL)
episode_metadata = {}

# When the configured series has a geo-blocked instalment, substitute the
# same-day instalment from the fallback series. Empirically Dagsrevyen for
# utlandet only publishes on the days Dagsrevyen is geo-blocked, so the
# mapping is 1:1 by release date.
GEO_FALLBACK_SERIES = {"dagsrevyen": "dagsrevyen-for-utlandet"}

# Map our public-facing slug (used for cover filenames and RSS URLs) to the
# upstream NRK series ID. Slugs not listed here are passed through unchanged.
NRK_SERIES_ID = {
    "dagsnytt-18": "dagsnytt-atten-tv",
    "nyhetsmorgen": "nyhetsmorgen-tv",
}


def _bunny_remote_path(prefix, episode_date):
    return f"{prefix}/{episode_date.strftime('%Y-%m-%d')}.mp4"


def _bunny_public_url(cdn_base, prefix, episode_date):
    return f"{cdn_base.rstrip('/')}/{_bunny_remote_path(prefix, episode_date)}"


def _list_existing_mp4s(bunny, prefix):
    """Return {filename: size_bytes} of MP4s already under `prefix/` on Bunny."""
    existing = {}
    for entry in bunny.list(prefix):
        name = entry.get("ObjectName", "")
        if name.endswith(".mp4"):
            existing[name] = int(entry.get("Length", 0) or 0)
    return existing


def _ensure_mp4(
    bunny, existing_mp4s, prefix, cdn_base, episode_date, hls_master_url,
    chapters=None, duration_seconds=None, subtitles=None,
):
    """
    Ensure an MP4 for `episode_date` exists on Bunny Storage under `prefix/`.

    If already present, return (public_url, size) from the directory listing.
    Otherwise mux the highest HLS variant and upload. Returns None on failure.

    `chapters` + `duration_seconds` are forwarded to the muxer so the MP4
    carries native chapter atoms; `subtitles` (a list from `get_subtitles`)
    causes each WebVTT to be embedded as a `mov_text` track that Apple
    Podcasts surfaces in the captions menu.
    """
    filename = f"{episode_date.strftime('%Y-%m-%d')}.mp4"
    public_url = _bunny_public_url(cdn_base, prefix, episode_date)

    if filename in existing_mp4s and existing_mp4s[filename] > 0:
        return public_url, existing_mp4s[filename]

    variant_url = pick_best_variant(hls_master_url)
    tmp_path = f"/tmp/nrk-mp4-{filename}"
    try:
        size = mux_to_mp4(
            variant_url, tmp_path,
            chapters=chapters,
            total_duration_seconds=duration_seconds,
            subtitles=subtitles,
        )
        bunny.put(_bunny_remote_path(prefix, episode_date), tmp_path)
        existing_mp4s[filename] = size
        return public_url, size
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def _prune_stale_mp4s(bunny, prefix, keep_dates):
    """Remove MP4s under `prefix/` whose date is not in keep_dates."""
    keep_filenames = {d.strftime("%Y-%m-%d") + ".mp4" for d in keep_dates}
    for entry in bunny.list(prefix):
        name = entry.get("ObjectName", "")
        if not name.endswith(".mp4"):
            continue
        if name not in keep_filenames:
            try:
                bunny.delete(f"{prefix}/{name}")
            except Exception:
                logging.warning("  Could not delete stale MP4")


def format_npt(seconds):
    """Format seconds as Normal Play Time (HH:MM:SS or MM:SS)."""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)

    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    else:
        return f"{minutes}:{secs:02d}"


def normalize_episode_title(title):
    """
    Strip dynamic temporal prefix from NRK episode titles.

    NRK news programs use titles like "I dag · Actual Title" where the prefix
    changes over time (I dag → I går → Weekday → Date). This causes podcast
    apps to show duplicates when titles change. Stripping everything before
    the " · " separator keeps titles stable.

    Also strips leading "– " dash prefix that some episodes have.
    """
    separator = " · "
    if separator in title:
        title = title.split(separator, 1)[1]
    if title.startswith("– "):
        title = title[2:]
    return title


def generate_podcast_guid(feed_url):
    """
    Generate a UUIDv5 for podcast:guid based on feed URL.
    Uses the Podcasting 2.0 namespace UUID as the base.
    """
    PODCAST_GUID_NS = uuid.UUID('ead4c236-bf58-58c6-a2c6-a6b28d128cb6')
    # Strip protocol and trailing slashes per spec
    url_stripped = feed_url.replace('https://', '').replace('http://', '').rstrip('/')
    return str(uuid.uuid5(PODCAST_GUID_NS, url_stripped))


def generate_chapters_json(series_id, episode_date, episode_title, chapters, series_title=None):
    """
    Generate a Podcasting 2.0 JSON chapters file.

    Args:
        series_id: The TV series identifier
        episode_date: Publication date of the episode (datetime)
        episode_title: Title of the episode
        chapters: List of chapter dicts with title, start_seconds, image_url
        series_title: Title of the series (optional)

    Returns:
        Tuple of (filename, cdn_url) or (None, None) if no chapters
    """
    if not chapters:
        return None, None

    date_str = episode_date.strftime("%Y-%m-%d")
    filename = f"{series_id}-{date_str}.json"
    filepath = f"{CHAPTERS_DIR}/{filename}"
    cdn_url = f"{JSDELIVR_BASE}/{filename}"

    # Build JSON chapters structure per Podcasting 2.0 spec
    json_chapters = {
        "version": "1.2.0",
        "title": episode_title,
        "author": "NRK",
        "podcastName": series_title or series_id.replace("-", " ").title(),
        "chapters": []
    }

    for ch in chapters:
        chapter_entry = {
            "startTime": ch["start_seconds"],
            "title": ch["title"]
        }
        if ch.get("image_url"):
            chapter_entry["img"] = ch["image_url"]
        json_chapters["chapters"].append(chapter_entry)

    # Write JSON file
    os.makedirs(CHAPTERS_DIR, exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(json_chapters, f, ensure_ascii=False, indent=2)

    logging.info(f"  Generated chapters JSON: {filename}")
    return filename, cdn_url


def add_podcasting2_tags_to_rss(
    rss_path, series_id, series_title=None,
    external_chapters=True, feed_url=None,
):
    """
    Add Podcasting 2.0 tags and Podlove Simple Chapters to RSS file.

    Channel-level tags added:
    - podcast:guid (UUIDv5 based on feed URL)
    - podcast:locked
    - podcast:medium (video)
    - podcast:person
    - itunes:author
    - itunes:category

    Item-level tags added:
    - psc:chapters (Podlove, inline for backward compatibility)
    - podcast:chapters (Podcasting 2.0, external JSON reference)
    - podcast:alternateEnclosure
    - podcast:person

    Args:
        rss_path: Path to RSS XML file
        series_id: The TV series identifier
        series_title: Title of the series (optional)
        external_chapters: When True (default), generate JSON chapter files
            under docs/chapters/ and reference them via <podcast:chapters>.
            When False, only inline <psc:chapters> is emitted (no files are
            written anywhere) — used by callers that must not leak chapter
            artefacts into the public docs tree.
        feed_url: Explicit feed URL used to derive <podcast:guid>. When
            omitted, defaults to the public GitHub Pages URL pattern.
    """
    # Parse the RSS file
    tree = ET.parse(rss_path)
    root = tree.getroot()

    # Register namespaces
    PSC_NS = 'http://podlove.org/simple-chapters'
    ITUNES_NS = 'http://www.itunes.com/dtds/podcast-1.0.dtd'
    ET.register_namespace('psc', PSC_NS)
    ET.register_namespace('podcast', PODCAST_NS)
    ET.register_namespace('itunes', ITUNES_NS)

    channel = root.find('channel')
    if channel is not None:
        # Generate podcast:guid from the supplied feed URL (or default to the
        # public GitHub Pages URL pattern for series that publish there).
        guid_feed_url = feed_url or f"https://bennokress.github.io/nrk-pod-feeds/rss/video/{series_id}.xml"
        guid = generate_podcast_guid(guid_feed_url)

        # Add channel-level Podcasting 2.0 tags if not present
        def add_if_missing(tag_name, text, ns=PODCAST_NS):
            if channel.find(f'{{{ns}}}{tag_name}') is None:
                elem = ET.SubElement(channel, f'{{{ns}}}{tag_name}')
                elem.text = text
                return elem
            return None

        add_if_missing('guid', guid)
        add_if_missing('locked', 'yes')
        add_if_missing('medium', 'video')

        # Add podcast:person with attributes
        if channel.find(f'{{{PODCAST_NS}}}person') is None:
            person = ET.SubElement(channel, f'{{{PODCAST_NS}}}person')
            person.set('role', 'host')
            person.set('href', 'https://www.nrk.no')
            person.text = 'NRK'

        # Add iTunes tags
        add_if_missing('author', 'NRK', ITUNES_NS)

        if channel.find(f'{{{ITUNES_NS}}}category') is None:
            cat = ET.SubElement(channel, f'{{{ITUNES_NS}}}category')
            cat.set('text', 'News')

    chapters_added = 0

    for item in root.findall('.//item'):
        enclosure = item.find('enclosure')
        if enclosure is None:
            continue

        url = enclosure.get('url', '')
        chapters = episode_chapters.get(url, [])
        metadata = episode_metadata.get(url, {})

        # Add Podlove Simple Chapters (inline, backward compatibility) if we have chapters
        if chapters:
            psc_chapters = ET.SubElement(item, f'{{{PSC_NS}}}chapters')
            psc_chapters.set('version', '1.2')

            for ch in chapters:
                psc_chapter = ET.SubElement(psc_chapters, f'{{{PSC_NS}}}chapter')
                psc_chapter.set('start', format_npt(ch['start_seconds']))
                psc_chapter.set('title', ch['title'])
                if ch.get('image_url'):
                    psc_chapter.set('image', ch['image_url'])

            # Optionally generate the external JSON chapters file and reference
            # it via <podcast:chapters>. Disabled by callers (e.g. the personal
            # overlay) that must not write artefacts under docs/.
            if external_chapters and metadata.get('date'):
                filename, cdn_url = generate_chapters_json(
                    series_id,
                    metadata['date'],
                    metadata.get('title', ''),
                    chapters,
                    series_title
                )
                if cdn_url:
                    chapters_elem = ET.SubElement(item, f'{{{PODCAST_NS}}}chapters')
                    chapters_elem.set('url', cdn_url)
                    chapters_elem.set('type', 'application/json+chapters')

            chapters_added += 1

        # Add podcast:alternateEnclosure tags
        enc_type = enclosure.get('type', '')
        enc_length = enclosure.get('length', '0')
        hls_url = metadata.get('hls_url')

        if enc_type == 'video/mp4':
            # MP4 is the primary enclosure: mirror it in alternateEnclosure as default,
            # and add the HLS URL as a non-default alternate for Podcasting 2.0 clients
            # that prefer streaming.
            alt_mp4 = ET.SubElement(item, f'{{{PODCAST_NS}}}alternateEnclosure')
            alt_mp4.set('type', 'video/mp4')
            alt_mp4.set('length', enc_length)
            alt_mp4.set('default', 'true')
            alt_mp4.set('title', 'Progressive MP4')
            mp4_source = ET.SubElement(alt_mp4, f'{{{PODCAST_NS}}}source')
            mp4_source.set('uri', url)

            if hls_url:
                alt_hls = ET.SubElement(item, f'{{{PODCAST_NS}}}alternateEnclosure')
                alt_hls.set('type', 'application/x-mpegURL')
                alt_hls.set('length', '0')
                alt_hls.set('title', 'HLS Video Stream')
                hls_source = ET.SubElement(alt_hls, f'{{{PODCAST_NS}}}source')
                hls_source.set('uri', hls_url)
        else:
            # HLS-only fallback (current behavior for series without mp4_enclosure).
            alt_enc = ET.SubElement(item, f'{{{PODCAST_NS}}}alternateEnclosure')
            alt_enc.set('type', 'application/x-mpegURL')
            alt_enc.set('length', '0')
            alt_enc.set('default', 'true')
            alt_enc.set('title', 'HLS Video Stream')
            source = ET.SubElement(alt_enc, f'{{{PODCAST_NS}}}source')
            source.set('uri', url)

        # Add podcast:person at item level
        person = ET.SubElement(item, f'{{{PODCAST_NS}}}person')
        person.set('role', 'host')
        person.set('href', 'https://www.nrk.no')
        person.text = 'NRK'

    # Write back the modified XML
    tree.write(rss_path, encoding='UTF-8', xml_declaration=True)
    logging.info(f"  Added Podcasting 2.0 tags and chapters to {chapters_added} episodes")


def get_podcast_image(series_id, nrk_id=None):
    """Get podcast image: use local square image if available, else API image.

    Where `series_id` differs from `nrk_id`, the slug-specific PNG won't
    exist; fall back to the upstream NRK ID's local PNG before going to the
    API image. This keeps cover artwork consistent across slugs that share
    an upstream source.
    """
    local_image_path = f"docs/assets/images/{series_id}-square.png"
    if os.path.exists(local_image_path):
        return f"{web_url}/assets/images/{series_id}-square.png"
    if nrk_id and nrk_id != series_id:
        nrk_local_path = f"docs/assets/images/{nrk_id}-square.png"
        if os.path.exists(nrk_local_path):
            return f"{web_url}/assets/images/{nrk_id}-square.png"
    # Fallback to API image (16:9) — needs the upstream NRK series ID
    return get_series_image(nrk_id or series_id)


def get_video_feed(
    series_id, season, feeds_dir, ep_count=10,
    mp4_enabled=False, bunny_prefix=None, cdn_base=None, bunny_client=None,
):
    """
    Generate a video podcast feed for a TV series.

    `series_id` is our public-facing slug (used for the RSS filename, cover
    image lookup, and chapter JSON paths). The upstream NRK series identifier
    is looked up via NRK_SERIES_ID, defaulting to `series_id` itself.

    When `mp4_enabled` is True, each episode's HLS source is muxed into a
    progressive MP4 and uploaded to Bunny Storage so podcatchers (notably
    Apple Podcasts) treat episodes as Video with Download/Auto-download
    enabled. The caller may supply `bunny_client`, `bunny_prefix`, and
    `cdn_base` to control where MP4s are stored and how their public URLs are
    constructed; if omitted, they fall back to the BUNNY_STORAGE_* env vars
    and `series_id` as the prefix.
    """
    nrk_id = NRK_SERIES_ID.get(series_id, series_id)
    existing_feed = get_last_feed(feeds_dir, series_id)

    bunny = bunny_client
    prefix = bunny_prefix or series_id
    base = cdn_base
    existing_mp4s = {}
    kept_dates = set()
    if mp4_enabled and bunny is None:
        bunny_zone = os.getenv("BUNNY_STORAGE_ZONE_NAME")
        bunny_key = os.getenv("BUNNY_STORAGE_ACCESS_KEY")
        if bunny_zone and bunny_key:
            bunny = BunnyStorage(bunny_zone, bunny_key)
        else:
            logging.warning(
                "  mp4 enclosure requested but BUNNY_STORAGE_* env vars missing; "
                "falling back to HLS enclosure"
            )

    if mp4_enabled and bunny is not None:
        if not base:
            logging.warning(
                "  mp4 enclosure requested but no CDN base supplied; "
                "falling back to HLS enclosure"
            )
            bunny = None
        else:
            try:
                existing_mp4s = _list_existing_mp4s(bunny, prefix)
            except Exception:
                logging.warning(
                    "  Could not list existing MP4s on Bunny; will re-upload as needed"
                )

    last_feed_update = parser.parse("1970-01-01 00:00:01+00:00")
    existing_image = None
    if existing_feed is not None:
        for channel in existing_feed.findall('channel'):
            last_build_date_elem = channel.find('lastBuildDate')
            if last_build_date_elem is not None:
                last_build_date = last_build_date_elem.text
                last_feed_update = parser.parse(last_build_date)
                logging.debug(f"Feed was last built {last_feed_update}")
            # Extract existing image URL from itunes:image
            itunes_ns = '{http://www.itunes.com/dtds/podcast-1.0.dtd}'
            itunes_image = channel.find(f'{itunes_ns}image')
            if itunes_image is not None:
                existing_image = itunes_image.get('href')

    # Get series metadata
    original_title = get_series_title(nrk_id)
    if not original_title:
        logging.info(f"Unable to get title for TV series {series_id}")
        return None

    # NRK suffixes the TV variant of radio shows with " - TV" (e.g. "Dagsnytt 18 - TV").
    # Strip it so the podcast title reads as users know the programme.
    if original_title.endswith(" - TV"):
        original_title = original_title[: -len(" - TV")]

    image = get_podcast_image(series_id, nrk_id)
    website = f"https://tv.nrk.no/serie/{nrk_id}"

    # Check if channel info changed
    channel_changed = False
    if existing_image and image != existing_image:
        logging.info(f"  Channel image changed: {existing_image} -> {image}")
        channel_changed = True

    logging.info(f"Processing TV series: {original_title}")
    logging.debug(f"  Title: {original_title}")
    logging.debug(f"  Image: {image}")

    p = Podcast(
        generator=podgen_agent,
        website=web_url,
        image=image,
        explicit=False,
        language="no"
    )

    # Iterate through instalments until we have enough valid episodes
    # This handles cases where some episodes are unavailable (geo-restricted, not yet playable, etc.)
    logging.info(f"Collecting {ep_count} valid episodes for TV series {series_id}...")

    new_episode = False
    valid_episodes = 0
    checked_episodes = 0

    fallback_series = GEO_FALLBACK_SERIES.get(series_id)

    for inst in iter_latest_instalments(nrk_id, playable_only=True):
        checked_episodes += 1

        # Substitute geo-blocked instalments with the same-day fallback (e.g. the
        # international edition) so the feed stays playable from outside Norway.
        if fallback_series and is_geo_blocked(inst):
            release = inst.get("releaseDateOnDemand")
            if release:
                try:
                    target_date = parser.parse(release).date()
                except (ValueError, TypeError):
                    target_date = None
                if target_date is not None:
                    substitute = find_instalment_by_release_date(fallback_series, target_date)
                    if substitute is not None:
                        logging.info(
                            f"  Substituting {inst.get('prfId')} with {substitute.get('prfId')} "
                            f"for {target_date} (geo-block)"
                        )
                        inst = substitute

        program_id = inst.get("prfId")
        titles = inst.get("titles", {})
        episode_title = titles.get("title", "Unknown")
        episode_subtitle = titles.get("subtitle", "")

        # Check if this is a new episode (for determining if feed needs update)
        episode_date = inst.get("releaseDateOnDemand") or inst.get("firstTransmissionDateDisplayValue", "")
        if episode_date and not new_episode:
            try:
                if parser.parse(episode_date) >= last_feed_update:
                    logging.info(f"  Found new episode {episode_title} from {episode_date}")
                    new_episode = True
            except:
                new_episode = True  # If we can't parse date, assume it's new

        # Try to get HLS stream URL - this validates the episode is actually available
        manifest = get_program_manifest(program_id)
        if not manifest:
            logging.info(f"  Skipping {program_id} ({episode_title}) - no manifest")
            continue

        stream_result = get_hls_stream_url(manifest)
        if not stream_result:
            logging.info(f"  Skipping {program_id} ({episode_title}) - no HLS stream")
            continue

        # Episode is valid - process it
        logging.info(f"Episode #{valid_episodes} (checked {checked_episodes}):")

        hls_url, hls_mime = stream_result
        enclosure_url = hls_url
        enclosure_mime = hls_mime
        enclosure_size = 0

        # Get episode image
        images = inst.get("image", [])
        episode_image = None
        if images:
            for img in reversed(images):
                if img.get("url"):
                    episode_image = img.get("url")
                    break

        # Get duration
        duration_str = inst.get("duration", "")
        duration = parse_iso_duration(duration_str)
        if duration == 0:
            duration = inst.get("durationInSeconds", 0)

        # Get release date
        date = inst.get("releaseDateOnDemand") or inst.get("firstTransmissionDateDisplayValue", "")

        parsed_date = None
        if date:
            try:
                parsed_date = parser.parse(date)
            except Exception:
                parsed_date = None

        # Fetch chapters (index points) for this episode. Done before MP4
        # muxing so we can embed them as native MP4 chapter atoms (Apple
        # Podcasts reads chapters from the file, not from the RSS).
        chapters = get_index_points(program_id)
        if chapters:
            logging.info(f"  Found {len(chapters)} chapters")

        # Fetch subtitle tracks (Norwegian forced + full SDH). Only the
        # rehosted MP4 path consumes these; the public HLS feed exposes them
        # via NRK's own player only.
        subtitles = get_subtitles(manifest) if bunny is not None else []
        if subtitles:
            logging.info(f"  Found {len(subtitles)} subtitle track(s)")

        # If MP4 rehosting is enabled and we have a parseable date, ensure an
        # MP4 exists on Bunny Storage and swap the enclosure to point at it.
        if bunny is not None and parsed_date is not None:
            try:
                mp4_result = _ensure_mp4(
                    bunny, existing_mp4s, prefix, base, parsed_date.date(), hls_url,
                    chapters=chapters, duration_seconds=duration,
                    subtitles=subtitles,
                )
                if mp4_result is not None:
                    enclosure_url, enclosure_size = mp4_result
                    enclosure_mime = "video/mp4"
                    kept_dates.add(parsed_date.date())
            except Exception:
                logging.warning(
                    "  MP4 mux/upload failed; falling back to HLS enclosure for this episode"
                )

        # Store the chapter list keyed by the final enclosure URL so the
        # Podcasting 2.0 post-processor can emit inline <psc:chapters> too.
        if chapters:
            episode_chapters[enclosure_url] = chapters

        # Store episode metadata (keyed by the final enclosure URL so the
        # Podcasting 2.0 post-processor can find chapters + HLS fallback).
        if parsed_date is not None:
            episode_metadata[enclosure_url] = {
                'date': parsed_date,
                'title': normalize_episode_title(episode_title),
                'series_id': series_id,
                'hls_url': hls_url,
            }

        logging.info(f"  Episode title: {episode_title}")
        logging.info(f"  Episode duration: {duration}s")
        logging.info(f"  Episode date: {date}")
        logging.info(f"  Enclosure: {enclosure_mime} {enclosure_url[:80]}...")
        logging.debug(f"  Episode image URL: {episode_image}")

        # Create episode with the final enclosure (MP4 if rehosted, else HLS)
        episode = Episode(
            title=normalize_episode_title(episode_title),
            media=Media(
                enclosure_url, enclosure_size, type=enclosure_mime,
                duration=timedelta(seconds=duration)
            ),
            summary=episode_subtitle,
            image=episode_image
        )

        # Parse and set publication date
        if date:
            try:
                episode.publication_date = parser.parse(date)
            except:
                logging.debug(f"  Could not parse date: {date}")

        p.episodes.append(episode)
        valid_episodes += 1

        # Stop when we have enough valid episodes
        if valid_episodes >= ep_count:
            break

    if valid_episodes == 0:
        logging.info(f"No valid episodes found for TV series {series_id}")
        return None

    # Drop MP4s for episodes no longer in the current feed window. We do this
    # before the early-return below so storage stays clean even when the feed
    # didn't otherwise need a rebuild.
    if bunny is not None and kept_dates:
        try:
            _prune_stale_mp4s(bunny, prefix, kept_dates)
        except Exception:
            logging.warning("  Pruning Bunny MP4s failed")

    if not new_episode and not channel_changed:
        logging.info("  No new episodes or channel changes since feed was last updated")
        return None

    if valid_episodes < ep_count:
        logging.info(f"  Only found {valid_episodes} valid episodes (wanted {ep_count})")

    p.name = original_title
    if fallback_series:
        p.description = (
            f"Uoffisiell videostrøm fra {original_title}. "
            "På dager der hovedsendingen er geoblokkert utenfor Norge erstattes "
            "episoden automatisk med samme dags sending fra Dagsrevyen for utlandet. "
            f"Innholdet er opphavsrettsbeskyttet av NRK. Kun for personlig bruk. Se {website} for mer informasjon."
        )
    else:
        p.description = f"Uoffisiell videostrøm fra {original_title}. Innholdet er opphavsrettsbeskyttet av NRK. Kun for personlig bruk. Se {website} for mer informasjon."

    return p


def write_video_xml(
    feeds_dir, series_id, podcast,
    external_chapters=True, feed_url=None,
):
    """Write video podcast RSS to file with Podcasting 2.0 tags and chapters."""
    output_path = f"{feeds_dir}/{series_id}.xml"
    podcast.rss_file(output_path, minimize=False)

    # Add Podcasting 2.0 tags and chapters (both Podlove inline and JSON external)
    add_podcasting2_tags_to_rss(
        output_path, series_id, podcast.name,
        external_chapters=external_chapters, feed_url=feed_url,
    )

    logging.info(f"Video feed XML successfully written to file: {output_path}\n---")
    return output_path


def _run_personal_overlay_if_configured():
    """
    Generate an additional video feed configured exclusively via environment.

    Reads PERSONAL_FEED_CDN_BASE, PERSONAL_FEED_PATH_PREFIX,
    PERSONAL_FEED_SOURCE_SERIES, PERSONAL_FEED_SUBSCRIPTION_SLUG plus the
    existing BUNNY_STORAGE_*. Returns silently if any are missing. Nothing
    about this overlay (slug, hostname, prefix, source series) appears
    anywhere in the repository.

    Logging is deliberately generic: a casual reader of the public CI logs
    sees that "an overlay step ran" but no concrete identifier.
    """
    cdn_base = os.getenv("PERSONAL_FEED_CDN_BASE")
    prefix = os.getenv("PERSONAL_FEED_PATH_PREFIX")
    source_series = os.getenv("PERSONAL_FEED_SOURCE_SERIES")
    subscription_slug = os.getenv("PERSONAL_FEED_SUBSCRIPTION_SLUG")
    if not (cdn_base and prefix and source_series and subscription_slug):
        return

    bunny_zone = os.getenv("BUNNY_STORAGE_ZONE_NAME")
    bunny_key = os.getenv("BUNNY_STORAGE_ACCESS_KEY")
    if not (bunny_zone and bunny_key):
        logging.info("[overlay] storage credentials missing; skipping")
        return

    bunny = BunnyStorage(bunny_zone, bunny_key)

    logging.info("[overlay] starting")
    with tempfile.TemporaryDirectory() as tmpdir:
        # Seed the temp dir with the previously published XML, if any, so the
        # new-episode detection in get_video_feed compares against the right
        # baseline. A 404 just means this is the first run.
        seed_path = os.path.join(tmpdir, f"{source_series}.xml")
        feed_url = f"{cdn_base.rstrip('/')}/{subscription_slug}"
        try:
            prev = requests.get(
                feed_url, headers={"User-Agent": f"nrk-pod-feeder {get_version()}"},
                timeout=15,
            )
            if prev.ok and prev.content:
                with open(seed_path, "wb") as f:
                    f.write(prev.content)
        except Exception:
            pass  # first-run or transient — proceed without baseline

        episode_chapters.clear()
        episode_metadata.clear()

        feed = get_video_feed(
            source_series, None, tmpdir, ep_count=5,
            mp4_enabled=True,
            bunny_prefix=prefix,
            cdn_base=cdn_base,
            bunny_client=bunny,
        )
        if not feed:
            logging.info("[overlay] no changes; nothing to upload")
            return

        write_video_xml(
            tmpdir, source_series, feed,
            external_chapters=False,
            feed_url=feed_url,
        )

        try:
            bunny.put(
                subscription_slug, seed_path,
                content_type="application/rss+xml",
            )
        except Exception:
            logging.warning("[overlay] feed XML upload failed")
            return

    logging.info("[overlay] done")


if __name__ == '__main__':
    init()

    feeds_dir = "docs/rss/video"

    # Ensure directories exist
    os.makedirs(feeds_dir, exist_ok=True)
    os.makedirs(CHAPTERS_DIR, exist_ok=True)

    programs = get_podcasts_config(tv_programs_cfg_file)

    for p in programs:
        if not p["enabled"]:
            continue

        series_id = p["id"]
        series_season = p.get("season")
        ep_count = p.get("episodes", 10)
        mp4_enabled = p.get("mp4_enclosure", False)

        # Clear episode data for each series to avoid leakage
        episode_chapters.clear()
        episode_metadata.clear()

        feed = get_video_feed(
            series_id, series_season, feeds_dir, ep_count, mp4_enabled=mp4_enabled
        )
        if not feed:
            logging.debug(f"Got empty result when fetching TV series {series_id}")
            continue

        write_video_xml(feeds_dir, series_id, feed)

    _run_personal_overlay_if_configured()

    logging.info("Done")
