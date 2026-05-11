import logging
import os
import requests

from common.helpers import get_version


_API_BASE = "https://storage.bunnycdn.com"
_HEADERS_BASE = {
    "User-Agent": f"nrk-pod-feeder {get_version()}",
    "accept": "application/json",
}


class BunnyStorage:
    """
    Minimal client for the Bunny Edge Storage HTTP API.

    Endpoints used:
      PUT    {base}/{zone}/{path}        upload a file
      DELETE {base}/{zone}/{path}        remove a file
      GET    {base}/{zone}/{prefix}/     list directory (JSON)
      GET    {base}/{zone}/{path}        existence check via HEAD-like GET
    """

    def __init__(self, zone, access_key, api_base=_API_BASE):
        self.zone = zone
        self.access_key = access_key
        self.api_base = api_base.rstrip("/")

    def _headers(self, extra=None):
        h = dict(_HEADERS_BASE)
        h["AccessKey"] = self.access_key
        if extra:
            h.update(extra)
        return h

    def _url(self, remote_path):
        return f"{self.api_base}/{self.zone}/{remote_path.lstrip('/')}"

    def put(self, remote_path, local_path, content_type="application/octet-stream"):
        """Upload `local_path` to `{zone}/{remote_path}`. Returns byte count.

        `content_type` is forwarded to Bunny Storage on PUT and is what Bunny
        will serve back via the bound pull zone, so callers can ensure e.g.
        `application/rss+xml` for feed XML uploads.
        """
        size = os.path.getsize(local_path)
        with open(local_path, "rb") as f:
            r = requests.put(
                self._url(remote_path),
                headers=self._headers({"Content-Type": content_type}),
                data=f,
                timeout=300,
            )
        if not r.ok:
            logging.warning(f"Bunny PUT failed ({r.status_code})")
            r.raise_for_status()
        logging.info(f"  Uploaded {size:,} bytes to Bunny Storage")
        return size

    def delete(self, remote_path):
        r = requests.delete(
            self._url(remote_path), headers=self._headers(), timeout=30
        )
        if r.status_code == 404:
            return False
        if not r.ok:
            logging.warning(f"Bunny DELETE failed ({r.status_code})")
            r.raise_for_status()
        logging.info("  Deleted file from Bunny Storage")
        return True

    def list(self, prefix):
        """List objects under `{zone}/{prefix}/`. Returns Bunny's raw JSON list."""
        # Bunny returns a directory listing when the path ends with /
        url = self._url(prefix.rstrip("/") + "/")
        r = requests.get(url, headers=self._headers(), timeout=30)
        if r.status_code == 404:
            return []
        if not r.ok:
            logging.warning(f"Bunny LIST failed ({r.status_code})")
            r.raise_for_status()
        return r.json() or []

    def exists(self, remote_path):
        """Return True if `{zone}/{remote_path}` exists. Uses a HEAD-equivalent GET."""
        r = requests.head(
            self._url(remote_path), headers=self._headers(), timeout=30, allow_redirects=False
        )
        if r.status_code == 200:
            return True
        if r.status_code == 404:
            return False
        # Some Bunny edges don't expose HEAD cleanly; fall back to GET with range
        r = requests.get(
            self._url(remote_path),
            headers=self._headers({"Range": "bytes=0-0"}),
            timeout=30,
            stream=True,
        )
        try:
            return r.status_code in (200, 206)
        finally:
            r.close()
