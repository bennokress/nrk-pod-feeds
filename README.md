# NRK News as Video Podcasts

![update-video-feeds badge](https://github.com/bennokress/nrk-pod-feeds/actions/workflows/update_video_feeds.yml/badge.svg)
![ci badge](https://github.com/bennokress/nrk-pod-feeds/actions/workflows/ci.yml/badge.svg)

Unofficial video podcast feeds for NRK's news programmes. Each feed carries the latest 10 episodes and is refreshed hourly via GitHub Actions. The main Dagsrevyen feed automatically substitutes the same-day international edition on days when the regular broadcast is geo-blocked outside Norway, so the feed stays playable from any region. **[You can subscribe to the feeds from the feed page](https://bennokress.github.io/nrk-pod-feeds/)**.

## Why did I build this

I live abroad and I'm learning Norwegian. Listening to people speak helps a lot with comprehension. While structured learning material is great in the beginning, hearing real people from different parts of Norway give interviews takes me a step closer to understanding Norwegians when I'm actually in Norway. The NRK TV app is good, but having a single unified feed that transparently switches between Dagsrevyen and Dagsrevyen utenfor Norge is the most reliable way to get my daily news fix from where I am.

## Technical details

HLS video (`application/vnd.apple.mpegurl`) wrapped as a Podcasting 2.0 RSS feed:

- `<podcast:alternateEnclosure>` — the HLS stream
- `<podcast:chapters>` — external JSON chapters with thumbnails (hosted via jsDelivr)
- `<psc:chapters>` — inline [Podlove Simple Chapters](https://podlove.org/simple-chapters/) as a fallback

Works with podcast apps that support video and Podcasting 2.0. See the [Podcast Index app directory](https://podcastindex.org/apps) for a list and look for apps with `video: true`.

The geo-aware Dagsrevyen feed is rebuilt hourly. When NRK marks the regular 7pm stream as geo-blocked outside Norway, the generator transparently substitutes the one from the *Dagsrevyen for utlandet* series so the feed remains playable from any region.

## Acknowledgements

Originally forked from [sindrel/nrk-pod-feeds](https://github.com/sindrel/nrk-pod-feeds), which provides automated RSS feeds for NRK audio podcasts. This repository [cut ties](https://github.com/sindrel/nrk-pod-feeds/issues/153) with the original, but adapts the same approach for NRK news video feeds and is maintained independently.
