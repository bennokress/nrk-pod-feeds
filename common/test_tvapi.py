import logging

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
