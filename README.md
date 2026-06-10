# arr-qbt-sync

This project was made for a Docker media server where Radarr, Sonarr and
qBittorrent share several drives. Downloads may arrive on one drive while the
final movie or TV library chosen by Arr is on another.

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

## How it works

For a movie, the script receives the source and imported paths from Radarr. It
renames the file or torrent folder in qBittorrent, changes its save location
when the library is on another drive, and verifies the result before cleaning
the old source.

TV season packs need more work because Sonarr imports each episode separately.
The script records each import event and waits until every video file in the
torrent has a destination. It then:

- places every episode in the correct show and season folder;
- keeps the season-folder naming already used by the show, or creates a
  `Season 01` style folder when needed;
- renames each file inside the torrent to match Sonarr's imported path;
- moves the torrent between drives when the show's library is elsewhere;
- moves remaining files such as subtitles and artwork into the destination;
- rechecks the complete torrent before removing the old folder.

This allows a season pack downloaded as one folder to become individual
episodes under the existing TV library while qBittorrent continues seeding the
same torrent from its new location.

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
