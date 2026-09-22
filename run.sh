#!/usr/bin/env bash
# Start the OBS todo overlay server. Add a Browser Source in OBS:
#   URL:    http://localhost:8787
#   Size:   480 x 900 (adjust to taste)
# Background is transparent, so it overlays cleanly on your stream.
cd "$(dirname "$0")"
exec python3 server.py "$@"
