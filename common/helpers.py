import os
import logging
import json
import xml.etree.ElementTree as ET

def init():
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(level=log_level)

def get_last_feed(feeds_dir, podcast_id):
    try:
        path = f"{feeds_dir}/{podcast_id}.xml"
        tree = ET.parse(path)
        root = tree.getroot()
        return root
    except:
        logging.info(f"No existing feed found for podcast {podcast_id}")
        return None

def get_podcasts_config(podcasts_cfg_file):
    with open(podcasts_cfg_file, 'r') as file:
        data = file.read()
        return json.loads(data)

def get_version():
    with open("version.txt") as file:
        return file.read()
