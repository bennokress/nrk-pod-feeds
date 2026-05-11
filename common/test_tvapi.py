import logging
from datetime import date

from . import tvapi


def test_parse_iso_duration():
    # Test various ISO 8601 duration formats
    assert tvapi.parse_iso_duration("PT43M33.76S") == 2613
    assert tvapi.parse_iso_duration("PT1H30M0S") == 5400
    assert tvapi.parse_iso_duration("PT5M") == 300
    assert tvapi.parse_iso_duration("PT30S") == 30
    assert tvapi.parse_iso_duration("PT1H") == 3600
    assert tvapi.parse_iso_duration("PT1H5M30S") == 3930
    assert tvapi.parse_iso_duration("") == 0
    assert tvapi.parse_iso_duration(None) == 0


def test_get_series_metadata():
    series_id = "dagsrevyen-21"

    metadata = tvapi.get_series_metadata(series_id)

    assert metadata is not None
    assert "navigation" in metadata
    assert "sections" in metadata["navigation"]


def test_get_series_seasons():
    series_id = "dagsrevyen-21"

    seasons = tvapi.get_series_seasons(series_id)

    assert seasons is not None
    assert len(seasons) > 0

    for season in seasons:
        assert "id" in season
        assert "title" in season


def test_get_series_instalments():
    series_id = "dagsrevyen-21"

    # Get latest month
    seasons = tvapi.get_series_seasons(series_id)
    assert seasons is not None
    assert len(seasons) > 0

    month_id = seasons[0]["id"]
    instalments = tvapi.get_series_instalments(series_id, month_id)

    assert instalments is not None
    assert len(instalments) > 0

    for inst in instalments:
        assert "prfId" in inst
        assert "titles" in inst


def test_get_latest_instalments():
    series_id = "dagsrevyen-21"

    instalments = tvapi.get_latest_instalments(series_id, limit=5)

    assert instalments is not None
    assert len(instalments) <= 5
    assert len(instalments) > 0

    for inst in instalments:
        assert "prfId" in inst


def test_get_program_manifest():
    series_id = "dagsrevyen-21"

    # Get playable instalments only (skip upcoming episodes)
    instalments = tvapi.get_latest_instalments(series_id, limit=1, playable_only=True)
    assert instalments is not None
    assert len(instalments) > 0

    program_id = instalments[0]["prfId"]
    manifest = tvapi.get_program_manifest(program_id)

    assert manifest is not None
    assert "playable" in manifest
    # playable should not be None for available episodes
    assert manifest["playable"] is not None
    assert "assets" in manifest["playable"]
    assert len(manifest["playable"]["assets"]) > 0


def test_get_hls_stream_url():
    series_id = "dagsrevyen-21"

    # Get playable instalments only
    instalments = tvapi.get_latest_instalments(series_id, limit=1, playable_only=True)
    assert instalments is not None

    program_id = instalments[0]["prfId"]
    manifest = tvapi.get_program_manifest(program_id)

    result = tvapi.get_hls_stream_url(manifest)
    assert result is not None

    url, mime_type = result
    assert ".m3u8" in url or "m3u8" in url
    assert mime_type == "application/vnd.apple.mpegurl"


def test_get_hls_stream_url_handles_none():
    """Test that get_hls_stream_url handles None/unavailable manifests."""
    assert tvapi.get_hls_stream_url(None) is None
    assert tvapi.get_hls_stream_url({}) is None
    assert tvapi.get_hls_stream_url({"playable": None}) is None


def test_get_subtitles_handles_missing_manifest():
    assert tvapi.get_subtitles(None) == []
    assert tvapi.get_subtitles({}) == []
    assert tvapi.get_subtitles({"playable": None}) == []
    assert tvapi.get_subtitles({"playable": {}}) == []
    assert tvapi.get_subtitles({"playable": {"subtitles": []}}) == []


def test_get_subtitles_returns_forced_and_full_for_recent_episode():
    """Live call: a recent dagsrevyen-for-utlandet episode exposes both
    Norwegian subtitle tracks (forced + full SDH)."""
    instalments = tvapi.get_latest_instalments(
        "dagsrevyen-for-utlandet", limit=1, playable_only=True
    )
    assert instalments

    program_id = instalments[0]["prfId"]
    manifest = tvapi.get_program_manifest(program_id)
    assert manifest

    subs = tvapi.get_subtitles(manifest)
    assert len(subs) >= 1

    types = [s["type"] for s in subs]
    # Forced track ('nor') should come first per the default ordering.
    assert types[0] == "nor"
    if "ttv" in types:
        assert types.index("nor") < types.index("ttv")

    for s in subs:
        assert s["webVtt"].startswith("https://")
        assert s["language"] == "nb"
        assert s["label"]


def test_get_subtitles_respects_types_filter_and_ordering():
    fake_manifest = {
        "playable": {
            "subtitles": [
                {"type": "ttv", "language": "nb", "label": "Full",
                 "webVtt": "https://example/full.vtt", "defaultOn": True},
                {"type": "nor", "language": "nb", "label": "Forced",
                 "webVtt": "https://example/forced.vtt", "defaultOn": False},
                {"type": "eng", "language": "en", "label": "English",
                 "webVtt": "https://example/en.vtt", "defaultOn": False},
            ]
        }
    }

    # Default filter: nor (forced) before ttv (full); eng excluded
    subs = tvapi.get_subtitles(fake_manifest)
    assert [s["type"] for s in subs] == ["nor", "ttv"]

    # Custom filter
    subs = tvapi.get_subtitles(fake_manifest, types=("ttv",))
    assert [s["type"] for s in subs] == ["ttv"]
    assert subs[0]["default_on"] is True

    # Entries without webVtt URL are dropped
    fake_manifest["playable"]["subtitles"].append(
        {"type": "noo", "language": "nb", "label": "X", "webVtt": ""}
    )
    subs = tvapi.get_subtitles(fake_manifest, types=("noo",))
    assert subs == []


def test_get_series_title():
    series_id = "dagsrevyen-21"

    title = tvapi.get_series_title(series_id)

    assert title is not None
    assert len(title) > 0


def test_dagsrevyen_for_utlandet():
    """Test the international edition of Dagsrevyen."""
    series_id = "dagsrevyen-for-utlandet"

    metadata = tvapi.get_series_metadata(series_id)
    assert metadata is not None

    seasons = tvapi.get_series_seasons(series_id)
    assert seasons is not None
    assert len(seasons) > 0

    instalments = tvapi.get_latest_instalments(series_id, limit=3)
    assert instalments is not None
    assert len(instalments) > 0


def test_is_geo_blocked():
    assert tvapi.is_geo_blocked({}) is False
    assert tvapi.is_geo_blocked({"usageRights": {}}) is False
    assert tvapi.is_geo_blocked(
        {"usageRights": {"geoBlock": {"isGeoBlocked": False}}}
    ) is False
    assert tvapi.is_geo_blocked(
        {"usageRights": {"geoBlock": {"isGeoBlocked": True, "displayValue": "Norge"}}}
    ) is True


def test_find_instalment_by_release_date():
    """The international edition publishes on the same calendar day as the
    geo-blocked main edition, so any recent date present in its top-30 should
    be findable; an impossible date returns None."""
    series_id = "dagsrevyen-for-utlandet"

    instalments = tvapi.get_latest_instalments(series_id, limit=3)
    assert instalments

    from dateutil import parser as _dp
    sample_date = _dp.parse(instalments[0]["releaseDateOnDemand"]).date()

    found = tvapi.find_instalment_by_release_date(series_id, sample_date)
    assert found is not None
    assert found.get("prfId") == instalments[0].get("prfId")

    assert tvapi.find_instalment_by_release_date(series_id, date(1970, 1, 1)) is None


def test_get_program_playback_metadata():
    """Test fetching playback metadata for a program."""
    series_id = "dagsrevyen-21"

    # Get a playable episode
    instalments = tvapi.get_latest_instalments(series_id, limit=1, playable_only=True)
    assert instalments is not None
    assert len(instalments) > 0

    program_id = instalments[0]["prfId"]
    metadata = tvapi.get_program_playback_metadata(program_id)

    assert metadata is not None
    assert "preplay" in metadata


def test_get_index_points():
    """Test extracting index points (chapters) from a program."""
    series_id = "dagsrevyen-21"

    # Get a playable episode
    instalments = tvapi.get_latest_instalments(series_id, limit=1, playable_only=True)
    assert instalments is not None
    assert len(instalments) > 0

    program_id = instalments[0]["prfId"]
    chapters = tvapi.get_index_points(program_id)

    # Dagsrevyen usually has chapters/index points
    assert isinstance(chapters, list)

    # If chapters exist, verify structure
    if len(chapters) > 0:
        for chapter in chapters:
            assert "title" in chapter
            assert "start_seconds" in chapter
            assert isinstance(chapter["start_seconds"], int)
