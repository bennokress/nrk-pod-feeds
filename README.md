[![Talk from JavaZone 2023](assets/vimeo.png)](https://vimeo.com/861697003)

# Open NRK Podcast Feeds
![sync-upstream badge](https://github.com/bennokress/nrk-pod-feeds/actions/workflows/sync_upstream.yml/badge.svg)
![update-video-feeds badge](https://github.com/bennokress/nrk-pod-feeds/actions/workflows/update_video_feeds.yml/badge.svg)
![python version badge](https://badgen.net/pypi/python/black)

Publishes RSS feeds with the last 10 episodes of every configured podcast, without delay. For personal use.  

## Feeds
**Go to [this page](https://bennokress.github.io/nrk-pod-feeds) for a list of available feeds.**

### Discovery  
New podcasts are discovered automatically. Changes are listed [here](DISCOVERY.md).  

### Archived feeds  
Some additional feeds include all episodes, such as Radioresepsjonen, Tazte Priv, etc.  

## How it works  
![A simplified sequence diagram](assets/nrk-pod-feeds.png?raw=true "Sequence Diagram")  

### Discovery routine  
* Runs once a day
* Auto-configures which podcasts to fetch
* Reduces API load and pipeline execution time

### Feed updates
* Runs every hour
* Fetches new episodes and adds them to RSS feeds

## Video Feeds

Video podcast feeds for NRK TV programs, currently featuring news broadcasts.

### Included Programs

| Program | Description |
|---------|-------------|
| **Dagsrevyen** | Main evening news (19:00) |
| **Dagsrevyen 21** | Late evening news (21:00) |
| **Dagsrevyen for utlandet** | International edition (available outside Norway/EU) |

### Feed Format

Video feeds use the [Podcasting 2.0](https://podcasting2.org) specification:

- **`<podcast:alternateEnclosure>`** - Marks the feed as video with HLS streaming
- **`<podcast:chapters>`** - External JSON chapters with thumbnails
- **`<psc:chapters>`** - Inline [Podlove Simple Chapters](https://podlove.org/simple-chapters/) for backward compatibility

### Compatible Apps

Any podcast app supporting Podcasting 2.0 video should work. See the [Podcast Index app directory](https://raw.githubusercontent.com/Podcastindex-org/web-ui/refs/heads/master/server/data/apps.json) for a list of compatible apps - look for apps with `"video": true` support.

### Adding New Programs

To add a new TV series:

1. Add an entry to `tv_programs.json`:
   ```json
   {
       "id": "series-id",
       "title": "De 10 siste fra Series Name",
       "season": null,
       "enabled": true,
       "type": "video"
   }
   ```

2. **(Optional)** Add a square channel image at `docs/assets/images/{series-id}.jpg` for better display in podcast apps. Without this, the 16:9 image from NRK is used.

## In the media  
* [kode24 (September 2023)](https://www.kode24.no/artikkel/nrk-slar-ned-pa-podcast-prosjekter-sindre-fikk-epost-for-foredrag/80166051)

## Contribute
Feel free to open a pull request or create an issue.

## Development
<details>
  <summary>Instructions</summary>

## Getting started
### Set up venv and install dependencies (Linux & MacOS)
```shell
python3 -m venv venv
source venv/bin/activate
python3 -m pip install -r requirements.txt
python3 -m pip install pytest
```

### Run tests
```shell
pytest -v --disable-warnings --log-cli-level=DEBUG
```

### Build or update podcast feeds
```shell
python3 generate_feeds.py
```

</details>
