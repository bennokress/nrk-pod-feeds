import json
import logging
import os
import uuid
import xml.etree.ElementTree as ET

from podgen import Podcast, Episode, Media
from dateutil import parser
from datetime import timedelta

from common.helpers import init, get_last_feed, get_podcasts_config, write_feeds_file, get_version
from common.tvapi import (
    get_series_metadata,
    get_series_title,
    get_series_image,
    get_latest_instalments,
    iter_latest_instalments,
    get_program_manifest,
    get_hls_stream_url,
    parse_iso_duration,
    get_index_points
)

podgen_agent = f"nrk-pod-feeder v{get_version()} (with help from python-podgen)"
tv_programs_cfg_file = "tv_programs.json"
web_url = "https://bennokress.github.io/nrk-pod-feeds"

# HLS video MIME type
VIDEO_MIME_TYPE = "application/vnd.apple.mpegurl"

# Track actual episode counts for dynamic titles
episode_counts = {}

# Track chapters for each episode (keyed by video URL)
episode_chapters = {}

# Podcasting 2.0 namespace (for medium tag)
PODCAST_NS = 'https://podcastindex.org/namespace/1.0'

# Podcasting 2.0 JSON chapters
JSDELIVR_BASE = "https://cdn.jsdelivr.net/gh/bennokress/nrk-pod-feeds@main/docs/chapters"
CHAPTERS_DIR = "docs/chapters"

# Track episode metadata for JSON chapters (keyed by video URL)
episode_metadata = {}


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


def add_podcasting2_tags_to_rss(rss_path, series_id, series_title=None):
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
        # Generate podcast:guid
        feed_url = f"https://bennokress.github.io/nrk-pod-feeds/rss/video/{series_id}.xml"
        guid = generate_podcast_guid(feed_url)

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

            # Generate JSON chapters file and add podcast:chapters reference
            if metadata.get('date'):
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

        # Add podcast:alternateEnclosure for video
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


def get_episode_count_from_xml(feeds_dir, series_id):
    """Read episode count from existing RSS XML file."""
    xml_path = f"{feeds_dir}/{series_id}.xml"
    if os.path.exists(xml_path):
        try:
            tree = ET.parse(xml_path)
            return len(tree.findall('.//item'))
        except Exception as e:
            logging.debug(f"Could not parse XML for episode count: {e}")
    return 10  # Default fallback


def get_podcast_image(series_id):
    """Get podcast image: use local square image if available, else API image."""
    local_image_path = f"docs/assets/images/{series_id}.jpg"
    if os.path.exists(local_image_path):
        # Use the GitHub Pages URL for the local image
        return f"{web_url}/assets/images/{series_id}.jpg"
    # Fallback to API image (16:9)
    return get_series_image(series_id)


def get_video_feed(series_id, season, feeds_dir, ep_count=10):
    """
    Generate a video podcast feed for a TV series.
    """
    existing_feed = get_last_feed(feeds_dir, series_id)

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
    original_title = get_series_title(series_id)
    if not original_title:
        logging.info(f"Unable to get title for TV series {series_id}")
        return None

    image = get_podcast_image(series_id)
    website = f"https://tv.nrk.no/serie/{series_id}"

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
        withhold_from_itunes=True,
        explicit=False,
        language="no"
    )

    # Iterate through instalments until we have enough valid episodes
    # This handles cases where some episodes are unavailable (geo-restricted, not yet playable, etc.)
    logging.info(f"Collecting {ep_count} valid episodes for TV series {series_id}...")

    new_episode = False
    valid_episodes = 0
    checked_episodes = 0

    for inst in iter_latest_instalments(series_id, playable_only=True):
        checked_episodes += 1
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

        video_url, video_mime = stream_result

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

        # Fetch chapters (index points) for this episode
        chapters = get_index_points(program_id)
        if chapters:
            episode_chapters[video_url] = chapters
            logging.info(f"  Found {len(chapters)} chapters")

        # Store episode metadata for Podcasting 2.0 JSON chapters
        if date:
            try:
                parsed_date = parser.parse(date)
                episode_metadata[video_url] = {
                    'date': parsed_date,
                    'title': normalize_episode_title(episode_title),
                    'series_id': series_id
                }
            except:
                pass

        logging.info(f"  Episode title: {episode_title}")
        logging.info(f"  Episode duration: {duration}s")
        logging.info(f"  Episode date: {date}")
        logging.info(f"  Video URL: {video_url[:80]}...")
        logging.debug(f"  Episode image URL: {episode_image}")

        # Create episode with video enclosure
        episode = Episode(
            title=normalize_episode_title(episode_title),
            media=Media(video_url, 0, type=video_mime, duration=timedelta(seconds=duration)),
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

    if not new_episode and not channel_changed:
        logging.info("  No new episodes or channel changes since feed was last updated")
        return None

    if valid_episodes < ep_count:
        logging.info(f"  Only found {valid_episodes} valid episodes (wanted {ep_count})")

    episode_counts[series_id] = valid_episodes  # Track for dynamic titles

    p.name = original_title
    p.description = f"Uoffisiell videostrøm fra {original_title}. Innholdet er opphavsrettsbeskyttet av NRK. Kun for personlig bruk. Se {website} for mer informasjon."

    return p


def write_video_xml(feeds_dir, series_id, podcast):
    """Write video podcast RSS to file with Podcasting 2.0 tags and chapters."""
    output_path = f"{feeds_dir}/{series_id}.xml"
    podcast.rss_file(output_path, minimize=False)

    # Add Podcasting 2.0 tags and chapters (both Podlove inline and JSON external)
    add_podcasting2_tags_to_rss(output_path, series_id, podcast.name)

    logging.info(f"Video feed XML successfully written to file: {output_path}\n---")
    return output_path


def write_video_feeds_file(feeds_file, programs, feeds_dir):
    """Write video feeds JavaScript file for web UI with dynamic titles."""
    updated_programs = []
    for p in programs:
        program_copy = p.copy()
        series_id = p["id"]

        # Get episode count: from current run, or from existing XML
        if series_id in episode_counts:
            count = episode_counts[series_id]
        else:
            count = get_episode_count_from_xml(feeds_dir, series_id)

        # Extract series name from static title ("De X siste fra SERIES_NAME")
        original_title = p["title"]
        if " fra " in original_title:
            series_name = original_title.split(" fra ", 1)[-1]
        else:
            series_name = series_id.replace("-", " ").title()

        program_copy["title"] = f"De {count} siste fra {series_name}"
        updated_programs.append(program_copy)

    with open(feeds_file, "w") as f:
        str_data = json.dumps(updated_programs, ensure_ascii=False, indent=2)
        f.write(f"const videoFeeds = {str_data}")
    logging.info(f"Video feeds written to file: {feeds_file}")


if __name__ == '__main__':
    init()

    feeds_dir = "docs/rss/video"
    feeds_file = "docs/video_feeds.js"

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

        # Clear episode data for each series to avoid leakage
        episode_chapters.clear()
        episode_metadata.clear()

        feed = get_video_feed(series_id, series_season, feeds_dir, ep_count)
        if not feed:
            logging.debug(f"Got empty result when fetching TV series {series_id}")
            continue

        write_video_xml(feeds_dir, series_id, feed)

    write_video_feeds_file(feeds_file, programs, feeds_dir)
    logging.info("Done")
