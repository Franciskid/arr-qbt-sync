# arr-qbt-sync

Hardlinks only work when the download and library are on the same filesystem.
With multiple drives, a download in qBittorrent's default data folder cannot
be hardlinked into a Radarr or Sonarr library on another drive. Arr has to copy
it, wasting space, or move it, which breaks the path qBittorrent uses for
seeding.

This script runs as a Radarr or Sonarr custom script after an import. It updates
qBittorrent to use the imported file in its final library location. It then
rechecks the torrent and removes the old source only when the new path is
valid. The result is one library copy that can still be seeded, even when the
download and library are on different drives.

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
