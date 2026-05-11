# NRK Dagsrevyen Video Podcasts

![update-video-feeds badge](https://github.com/bennokress/nrk-pod-feeds/actions/workflows/update_video_feeds.yml/badge.svg)
![ci badge](https://github.com/bennokress/nrk-pod-feeds/actions/workflows/ci.yml/badge.svg)

Unofficial video podcast feeds for NRK's flagship news programme **Dagsrevyen** — main edition (19:00), late edition (21:00), and the international edition (viewable outside Norway and the EU). Each feed carries the latest 10 episodes, refreshed hourly via GitHub Actions. For personal use.

## Feeds

| Programme | Feed URL |
|---|---|
| Dagsrevyen (19:00) | <https://bennokress.github.io/nrk-pod-feeds/rss/video/dagsrevyen.xml> |
| Dagsrevyen 21 (21:00) | <https://bennokress.github.io/nrk-pod-feeds/rss/video/dagsrevyen-21.xml> |
| Dagsrevyen for utlandet | <https://bennokress.github.io/nrk-pod-feeds/rss/video/dagsrevyen-for-utlandet.xml> |

For a more comfortable subscribe experience (covers, copy buttons, one-click subscribe), open the **[feed page](https://bennokress.github.io/nrk-pod-feeds/)**.

## Feed format

HLS video (`application/vnd.apple.mpegurl`) wrapped as a Podcasting 2.0 RSS feed:

- `<podcast:alternateEnclosure>` — the HLS stream
- `<podcast:chapters>` — external JSON chapters with thumbnails (hosted via jsDelivr)
- `<psc:chapters>` — inline [Podlove Simple Chapters](https://podlove.org/simple-chapters/) as a fallback

Works with podcast apps that support video and Podcasting 2.0. See the [Podcast Index app directory](https://podcastindex.org/apps) for a list — look for apps with `video: true`. Tested with Pocket Casts.

## Development

```shell
python3 -m venv venv
source venv/bin/activate
python3 -m pip install -r requirements.txt
pytest -v
python3 generate_video_feeds.py
```

`tv_programs.json` lists the configured series. The pipeline is generic and works for any NRK TV series, but this repository deliberately ships only the three Dagsrevyen variants.

## Acknowledgements

Originally forked from [sindrel/nrk-pod-feeds](https://github.com/sindrel/nrk-pod-feeds), which provides automated RSS feeds for NRK *audio* podcasts. This fork adapts the same approach for **NRK Dagsrevyen video feeds** and is maintained independently.
