import os
import tempfile

import pytest

from . import bunny


_ZONE = os.getenv("BUNNY_STORAGE_ZONE_NAME")
_KEY = os.getenv("BUNNY_STORAGE_ACCESS_KEY")


pytestmark = pytest.mark.skipif(
    not (_ZONE and _KEY),
    reason="BUNNY_STORAGE_ZONE_NAME and BUNNY_STORAGE_ACCESS_KEY must be set",
)


def test_round_trip_put_list_delete():
    """End-to-end against a real Bunny Storage zone (env-gated)."""
    client = bunny.BunnyStorage(_ZONE, _KEY)

    payload = b"nrk-pod-feeder bunny round-trip test\n"
    remote_path = "_test/round-trip.txt"

    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        tmp.write(payload)
        local_path = tmp.name

    try:
        size = client.put(remote_path, local_path)
        assert size == len(payload)

        listing = client.list("_test")
        assert any(entry.get("ObjectName") == "round-trip.txt" for entry in listing)

        assert client.exists(remote_path) is True

        assert client.delete(remote_path) is True
        assert client.exists(remote_path) is False
    finally:
        os.unlink(local_path)
