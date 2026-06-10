# arr-qbt-sync

Radarr and Sonarr can import a download into the library, but qBittorrent may
keep pointing at the old download path. That leaves duplicate hardlinks,
broken seeding paths, or old folders that never get cleaned up.

This script runs as a Radarr or Sonarr custom script after an import. It updates
the torrent to use the final library path, asks qBittorrent to recheck it, then
removes the old source only when the new path is valid.

## Setup

1. Edit `config.json` so the Arr paths match the paths seen by qBittorrent.
2. Mount this directory in both Arr containers, for example at `/scripts`.
3. Add `/scripts/arr_qbt_sync.py` as a Custom Script notification for download
   and upgrade events.
4. Set `ARR_QBT_HOST`, `ARR_QBT_PORT`, `ARR_QBT_USERNAME`, and
   `ARR_QBT_PASSWORD` in the Arr containers.

Completed Download Handling can stay disabled if another part of your setup
starts the import. This script only handles the path synchronization after Arr
has imported the file.

To use another config file, set `ARR_QBT_SYNC_CONFIG`.
