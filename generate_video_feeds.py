import logging
import os
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
    get_program_manifest,
    get_hls_stream_url,
    parse_iso_duration
)

podgen_agent = f"nrk-pod-feeder v{get_version()} (with help from python-podgen)"
tv_programs_cfg_file = "tv_programs.json"
web_url = "https://bennokress.github.io/nrk-pod-feeds"

# HLS video MIME type
VIDEO_MIME_TYPE = "application/vnd.apple.mpegurl"

# Track actual episode counts for dynamic titles
episode_counts = {}


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
    if existing_feed:
        for channel in existing_feed.findall('channel'):
            last_build_date_elem = channel.find('lastBuildDate')
            if last_build_date_elem is not None:
                last_build_date = last_build_date_elem.text
                last_feed_update = parser.parse(last_build_date)
                logging.debug(f"Feed was last built {last_feed_update}")

    # Get series metadata
    original_title = get_series_title(series_id)
    if not original_title:
        logging.info(f"Unable to get title for TV series {series_id}")
        return None

    image = get_podcast_image(series_id)
    website = f"https://tv.nrk.no/serie/{series_id}"

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

    # Get latest instalments (playable only)
    instalments = get_latest_instalments(series_id, limit=ep_count, playable_only=True)

    if not instalments:
        logging.info(f"No instalments found for TV series {series_id}")
        return None

    # Check for new episodes
    new_episode = False
    for inst in instalments:
        episode_date = inst.get("releaseDateOnDemand") or inst.get("firstTransmissionDateDisplayValue", "")
        if episode_date:
            try:
                if parser.parse(episode_date) >= last_feed_update:
                    episode_title = inst.get("titles", {}).get("title", "Unknown")
                    logging.info(f"  Found new episode {episode_title} from {episode_date}")
                    new_episode = True
            except:
                new_episode = True  # If we can't parse date, assume it's new

    if not new_episode:
        logging.info("  No new episodes found since feed was last updated")
        return None

    ep_i = 0
    for inst in instalments:
        logging.info(f"Episode #{ep_i}:")

        program_id = inst.get("prfId")
        titles = inst.get("titles", {})
        episode_title = titles.get("title", "Unknown")
        episode_subtitle = titles.get("subtitle", "")

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

        # Get HLS stream URL
        manifest = get_program_manifest(program_id)
        if not manifest:
            logging.info(f"  Unable to get manifest for {program_id}")
            continue

        stream_result = get_hls_stream_url(manifest)
        if not stream_result:
            logging.info(f"  Unable to get HLS stream URL for {program_id}")
            continue

        video_url, video_mime = stream_result

        logging.info(f"  Episode title: {episode_title}")
        logging.info(f"  Episode duration: {duration}s")
        logging.info(f"  Episode date: {date}")
        logging.info(f"  Video URL: {video_url[:80]}...")
        logging.debug(f"  Episode image URL: {episode_image}")

        # Create episode with video enclosure
        episode = Episode(
            title=episode_title,
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
        ep_i += 1

    if ep_i == 0:
        logging.info(f"No valid episodes found for TV series {series_id}")
        return None

    episodes_c = len(p.episodes)
    episode_counts[series_id] = episodes_c  # Track for dynamic titles

    p.name = original_title
    p.description = f"Uoffisiell videostrøm fra {original_title}. Innholdet er opphavsrettsbeskyttet av NRK. Kun for personlig bruk. Se {website} for mer informasjon."

    return p


def write_video_xml(feeds_dir, series_id, podcast):
    """Write video podcast RSS to file."""
    output_path = f"{feeds_dir}/{series_id}.xml"
    podcast.rss_file(output_path, minimize=False)

    logging.info(f"Video feed XML successfully written to file: {output_path}\n---")
    return output_path


def write_video_feeds_file(feeds_file, programs, feeds_dir):
    """Write video feeds JavaScript file for web UI with dynamic titles."""
    import json

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

    programs = get_podcasts_config(tv_programs_cfg_file)

    for p in programs:
        if not p["enabled"]:
            continue

        series_id = p["id"]
        series_season = p.get("season")
        ep_count = p.get("episodes", 10)

        feed = get_video_feed(series_id, series_season, feeds_dir, ep_count)
        if not feed:
            logging.debug(f"Got empty result when fetching TV series {series_id}")
            continue

        write_video_xml(feeds_dir, series_id, feed)

    write_video_feeds_file(feeds_file, programs, feeds_dir)
    logging.info("Done")
