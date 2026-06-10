#!/usr/bin/env python3

import json
import logging
import os
import re
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.getenv("ARR_QBT_SYNC_CONFIG", SCRIPT_DIR / "config.json"))
LOG_DIR = Path(os.getenv("ARR_QBT_SYNC_STATE_DIR", "/config/arr-qbt-sync"))
LOG_DIR.mkdir(parents=True, exist_ok=True)

_logger = logging.getLogger("arr_qbt_sync")
_logger.setLevel(logging.DEBUG)
try:
	_log_path = LOG_DIR / "hook.log"
	if _log_path.exists() and not os.access(_log_path, os.W_OK):
		_log_path.unlink()
	_file_handler = logging.FileHandler(_log_path)
	_file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
	_logger.addHandler(_file_handler)
except OSError:
	pass


def load_config(path):
	try:
		config = json.loads(path.read_text())
	except (OSError, json.JSONDecodeError) as error:
		raise RuntimeError(f"Unable to load configuration from {path}: {error}") from error

	path_mappings = config.get("arr_to_qbt_paths")
	video_extensions = config.get("video_extensions")
	if not isinstance(path_mappings, dict) or not path_mappings:
		raise RuntimeError("config.json must define a non-empty arr_to_qbt_paths object")
	if not isinstance(video_extensions, list) or not video_extensions:
		raise RuntimeError("config.json must define a non-empty video_extensions list")

	return path_mappings, {
		extension.lower() if extension.startswith(".") else f".{extension.lower()}"
		for extension in video_extensions
	}


ARR_TO_QBT_PATHS, VIDEO_EXTENSIONS = load_config(CONFIG_PATH)


def log(message):
	_logger.info(message)
	print(f"[arr_qbt_sync] {message}")


def fail(message):
	_logger.error(message)
	print(f"[arr_qbt_sync] {message}")
	raise SystemExit(1)


def detect_app():
	if os.getenv("sonarr_eventtype"):
		return "sonarr"
	if os.getenv("radarr_eventtype"):
		return "radarr"
	return None


def longest_prefix_match(path_str, mapping):
	path_str = str(path_str)
	candidates = []
	for prefix, target in mapping.items():
		if path_str == prefix or path_str.startswith(prefix + "/"):
			candidates.append((len(prefix), prefix, target))
	if not candidates:
		return None, None
	_, prefix, target = max(candidates, key=lambda item: item[0])
	return prefix, target


def arr_to_qbt(path_str):
	if not path_str:
		return ""
	prefix, target = longest_prefix_match(path_str, ARR_TO_QBT_PATHS)
	if prefix is None:
		raise ValueError(f"No Arr->qBittorrent path mapping for: {path_str}")
	suffix = path_str[len(prefix):]
	return target + suffix


def qbt_library_root_for_arr_path(path_str):
	prefix, _ = longest_prefix_match(path_str, ARR_TO_QBT_PATHS)
	if prefix is None:
		raise ValueError(f"No library root mapping for: {path_str}")
	return ARR_TO_QBT_PATHS[prefix]


def is_arr_root_path(path_str):
	if not path_str:
		return False
	return any(Path(path_str) == Path(prefix) for prefix in ARR_TO_QBT_PATHS)


def is_root_level_file_source(source_path, source_folder):
	if not source_path or not source_folder:
		return False
	source_path_obj = Path(source_path)
	source_folder_obj = Path(source_folder)
	return source_path_obj.parent == source_folder_obj and source_path_obj.name != ""


def extract_season_number(text):
	if not text:
		return None
	match = re.search(r"(?i)(?:^|[ ._\-])s(\d{1,2})(?:$|[ ._\-])", text)
	if not match:
		return None
	return int(match.group(1))


def looks_like_season_pack_folder(folder_name):
	if not folder_name:
		return False
	season_number = extract_season_number(folder_name)
	if season_number is None:
		return False
	# Treat names with Sxx but without Exx as season-pack roots.
	return re.search(r"(?i)(?:^|[ ._\-])e\d{1,3}(?:$|[ ._\-])", folder_name) is None


def path_is_within(path_str, parent_str):
	try:
		Path(path_str).relative_to(Path(parent_str))
		return True
	except ValueError:
		return False


def resolve_canonical_season_folder_name(series_path, imported_path, source_folder, source_path):
	imported_parent_name = Path(imported_path).parent.name
	season_number = (
		extract_season_number(imported_parent_name)
		or extract_season_number(Path(source_folder).name)
		or extract_season_number(Path(source_path).name)
		or extract_season_number(Path(imported_path).name)
	)
	if season_number is None:
		return None

	series_root = Path(series_path)
	if series_root.exists():
		for child in sorted(series_root.iterdir()):
			if not child.is_dir():
				continue
			child_season = extract_season_number(child.name)
			if child_season == season_number:
				return child.name

	if re.fullmatch(r"(?i)season\s+0*\d+", imported_parent_name):
		return imported_parent_name

	return f"Season {season_number:02d}"


def normalize_sonarr_target_paths(source_folder, source_path, imported_path, series_path):
	"""
	Force a canonical Sonarr series root for season-pack imports without
	overriding Sonarr's existing season folder naming when the import path is
	already under the correct series root.
	"""
	if not series_path:
		return imported_path
	if path_is_within(imported_path, series_path):
		return imported_path

	source_folder_name = Path(source_folder).name
	if not looks_like_season_pack_folder(source_folder_name):
		return imported_path

	season_folder_name = resolve_canonical_season_folder_name(
		series_path, imported_path, source_folder, source_path
	)
	if not season_folder_name:
		return imported_path

	canonical = str(Path(series_path) / season_folder_name / Path(imported_path).name)
	log(
		"Canonicalized Sonarr import target for season-pack source: "
		f"{imported_path} -> {canonical}"
	)
	return canonical


def ensure_state_dir(app_name):
	state_dir = Path(f"/config/arr-qbt-sync/{app_name}")
	state_dir.mkdir(parents=True, exist_ok=True)
	return state_dir


def state_path(app_name, download_id):
	safe_id = download_id.lower()
	return ensure_state_dir(app_name) / f"{safe_id}.json"


def load_state(app_name, download_id):
	path = state_path(app_name, download_id)
	if not path.exists():
		return {"mappings": []}
	return json.loads(path.read_text())


def save_state(app_name, download_id, payload):
	path = state_path(app_name, download_id)
	path.write_text(json.dumps(payload, indent=2, sort_keys=True))


def clear_state(app_name, download_id):
	path = state_path(app_name, download_id)
	if path.exists():
		path.unlink()


def build_mapping(source_path_arr, imported_path_arr):
	return {
		"source_path_arr": source_path_arr,
		"source_path_qbt": arr_to_qbt(source_path_arr),
		"imported_path_arr": imported_path_arr,
		"imported_path_qbt": arr_to_qbt(imported_path_arr),
	}


def is_video_file(path_str):
	return Path(path_str).suffix.lower() in VIDEO_EXTENSIONS


def torrent_video_count(files):
	return sum(1 for item in files if is_video_file(item["name"]))


def move_remaining_source_items(source_folder, target_folder):
	source = Path(source_folder)
	target = Path(target_folder)
	if not source.exists():
		log(f"move_remaining: source does not exist: {source}")
		return
	target.mkdir(parents=True, exist_ok=True)
	children = list(source.iterdir())
	log(f"move_remaining: {len(children)} items in {source} -> {target}")
	for child in children:
		destination = target / child.name
		if destination.exists():
			log(f"move_remaining: skip (exists): {child.name}")
			continue
		shutil.move(str(child), str(destination))
		log(f"move_remaining: moved {child.name}")


def remove_source_folder(source_folder):
	source = Path(source_folder)
	if not source.exists():
		log(f"remove_source: does not exist: {source}")
		return
	remaining = list(source.rglob("*"))
	log(f"remove_source: deleting {source} ({len(remaining)} items remaining)")
	shutil.rmtree(source)
	log(f"remove_source: deleted {source}")


def remove_empty_parents(folder, stop_at):
	"""Remove empty parent directories up to (not including) stop_at."""
	current = Path(folder).parent
	stop = Path(stop_at)
	while current != stop and current != current.parent:
		try:
			current.rmdir()
			current = current.parent
		except OSError:
			break


def remove_root_level_source_links(source_folder, mappings):
	if not is_arr_root_path(source_folder):
		return

	for mapping in mappings:
		source = Path(mapping["source_path_arr"])
		imported = Path(mapping["imported_path_arr"])
		if source == imported or source.parent != Path(source_folder):
			continue
		if not source.exists() or not imported.exists():
			continue
		try:
			if not os.path.samefile(source, imported):
				log(f"Skipping root-level source cleanup; files differ: {source}")
				continue
		except OSError as error:
			log(f"Skipping root-level source cleanup for {source}: {error}")
			continue
		source.unlink()
		log(f"Removed imported root-level hardlink: {source}")


def common_root(file_names):
	split_names = [name.split("/") for name in file_names if name]
	if not split_names:
		return ""
	if any(len(parts) < 2 for parts in split_names):
		return ""

	prefix = []
	for parts in zip(*split_names):
		if len(set(parts)) != 1:
			break
		prefix.append(parts[0])
	return "/".join(prefix)


class QBittorrentClient:
	def __init__(self):
		self.host = os.getenv("ARR_QBT_HOST", "qbittorrent")
		self.port = os.getenv("ARR_QBT_PORT", "8080")
		self.username = os.getenv("ARR_QBT_USERNAME")
		self.password = os.getenv("ARR_QBT_PASSWORD")
		if not self.username or self.password is None:
			fail("ARR_QBT_USERNAME and ARR_QBT_PASSWORD must be set in Radarr/Sonarr containers")
		self.base_url = f"http://{self.host}:{self.port}"
		self.cookie = None

	def _request(self, path, method="GET", data=None):
		url = self.base_url + path
		headers = {}
		if self.cookie:
			headers["Cookie"] = self.cookie
		encoded = None
		if data is not None:
			encoded = urllib.parse.urlencode(data).encode()
		request = urllib.request.Request(url, data=encoded, headers=headers, method=method)
		with urllib.request.urlopen(request) as response:
			set_cookie = response.headers.get("Set-Cookie")
			if set_cookie:
				self.cookie = set_cookie.split(";", 1)[0]
			raw = response.read()
		return raw

	def _post_with_fallback(self, paths, data):
		last_error = None
		for path in paths:
			try:
				self._request(path, method="POST", data=data)
				return
			except urllib.error.HTTPError as error:
				if error.code != 404:
					raise
				last_error = error
		if last_error is not None:
			raise last_error

	def login(self):
		raw = self._request(
			"/api/v2/auth/login",
			method="POST",
			data={"username": self.username, "password": self.password},
		)
		if raw.decode(errors="ignore").strip() != "Ok.":
			fail("Failed to authenticate to qBittorrent")

	def get_torrent(self, download_id):
		raw = self._request(f"/api/v2/torrents/info?hashes={download_id}")
		data = json.loads(raw)
		if not data:
			fail(f"Torrent {download_id} not found in qBittorrent")
		return data[0]

	def get_files(self, download_id):
		raw = self._request(f"/api/v2/torrents/files?hash={download_id}")
		return json.loads(raw)

	def rename_folder(self, download_id, old_path, new_path):
		log(f"qBittorrent renameFolder: {old_path} -> {new_path}")
		self._request(
			"/api/v2/torrents/renameFolder",
			method="POST",
			data={"hash": download_id, "oldPath": old_path, "newPath": new_path},
		)

	def rename_file(self, download_id, old_path, new_path):
		log(f"qBittorrent renameFile: {old_path} -> {new_path}")
		self._request(
			"/api/v2/torrents/renameFile",
			method="POST",
			data={"hash": download_id, "oldPath": old_path, "newPath": new_path},
		)

	def set_location(self, download_id, location):
		log(f"qBittorrent setLocation: {location}")
		self._request(
			"/api/v2/torrents/setLocation",
			method="POST",
			data={"hashes": download_id, "location": location},
		)

	def recheck(self, download_id):
		self._request(
			"/api/v2/torrents/recheck",
			method="POST",
			data={"hashes": download_id},
		)

	def resume(self, download_id):
		self._post_with_fallback(
			["/api/v2/torrents/resume", "/api/v2/torrents/start"],
			data={"hashes": download_id},
		)

	def wait_for_recheck(self, download_id, timeout_seconds=300):
		deadline = time.time() + timeout_seconds
		start_deadline = min(deadline, time.time() + 15)
		saw_checking = False
		last_torrent = None
		while time.time() < deadline:
			last_torrent = self.get_torrent(download_id)
			state = last_torrent.get("state", "")
			is_checking = "checking" in state.lower()
			if is_checking:
				saw_checking = True
			elif saw_checking or time.time() >= start_deadline:
				return last_torrent
			time.sleep(2)
		return last_torrent or self.get_torrent(download_id)


def match_current_relative(file_names, desired_rel):
	if desired_rel in file_names:
		return desired_rel
	basename = os.path.basename(desired_rel)
	candidates = [name for name in file_names if os.path.basename(name) == basename]
	if len(candidates) == 1:
		return candidates[0]
	raise ValueError(f"Unable to uniquely match torrent file for desired path: {desired_rel}")


def augment_state_from_target_files(state, qbt_files):
	target_root = Path(state["target_root_arr"])
	if not target_root.exists():
		return False
	qbt_video_basenames = {
		os.path.basename(item["name"])
		for item in qbt_files
		if is_video_file(item["name"])
	}
	existing = {(entry["source_path_arr"], entry["imported_path_arr"]) for entry in state.get("mappings", [])}
	changed = False
	for target in sorted(target_root.glob("*")):
		if not target.is_file() or not is_video_file(target.name):
			continue
		if target.name not in qbt_video_basenames:
			continue
		source = str(Path(state["source_folder_arr"]) / target.name)
		key = (source, str(target))
		if key in existing:
			continue
		state.setdefault("mappings", []).append(build_mapping(source, str(target)))
		existing.add(key)
		changed = True
	return changed


def collect_event(app_name):
	event_type = os.getenv(f"{app_name}_eventtype", "")
	if event_type == "Test":
		log("Received test event")
		raise SystemExit(0)
	if event_type not in ("Download", "ImportComplete"):
		log(f"Ignoring unsupported event type: {event_type}")
		raise SystemExit(0)

	download_client = os.getenv(f"{app_name}_download_client", "")
	if download_client.lower() != "qbittorrent":
		log(f"Ignoring download client: {download_client}")
		raise SystemExit(0)

	download_id = os.getenv(f"{app_name}_download_id", "").strip().lower()
	if not download_id:
		fail("Missing download id")

	if app_name == "sonarr":
		source_path = os.getenv("sonarr_episodefile_sourcepath", "")
		source_folder = os.getenv("sonarr_episodefile_sourcefolder", "")
		imported_path = os.getenv("sonarr_episodefile_path", "")
		series_path = os.getenv("sonarr_series_path", "")
	else:
		source_path = os.getenv("radarr_moviefile_sourcepath", "")
		source_folder = os.getenv("radarr_moviefile_sourcefolder", "")
		imported_path = os.getenv("radarr_moviefile_path", "")
		series_path = ""

	if not source_path or not source_folder or not imported_path:
		fail("Missing source/import paths from Arr environment")

	root_level_file_source = is_arr_root_path(source_folder) and is_root_level_file_source(source_path, source_folder)
	if is_arr_root_path(source_folder) and not root_level_file_source:
		fail(
			f"Unsafe Arr event: source folder resolves to library root {source_folder}. "
			"Refusing to modify qBittorrent paths or move files."
		)
	if root_level_file_source:
		log(
			f"Allowing rename-only handling for root-level source file: "
			f"{source_path} -> {imported_path}"
		)

	if app_name == "sonarr":
		imported_path = normalize_sonarr_target_paths(source_folder, source_path, imported_path, series_path)

	return {
		"download_id": download_id,
		"source_path_arr": source_path,
		"source_folder_arr": source_folder,
		"imported_path_arr": imported_path,
		"source_path_qbt": arr_to_qbt(source_path),
		"source_folder_qbt": arr_to_qbt(source_folder),
		"imported_path_qbt": arr_to_qbt(imported_path),
		"target_root_arr": str(Path(imported_path).parent),
		"target_root_qbt": str(Path(arr_to_qbt(imported_path)).parent),
		"library_root_qbt": qbt_library_root_for_arr_path(imported_path),
	}


def merge_mapping(state, event):
	mappings = state.setdefault("mappings", [])
	entry = build_mapping(event["source_path_arr"], event["imported_path_arr"])
	if entry not in mappings:
		mappings.append(entry)

	state["source_folder_arr"] = event["source_folder_arr"]
	state["source_folder_qbt"] = event["source_folder_qbt"]
	state["target_root_arr"] = event["target_root_arr"]
	state["target_root_qbt"] = event["target_root_qbt"]
	state["library_root_qbt"] = event["library_root_qbt"]
	return state


def finalize_transfer(app_name, qbt, download_id, state):
	source_folder_arr = state["source_folder_arr"]
	target_root_arr = state["target_root_arr"]
	target_root_qbt = state["target_root_qbt"]
	library_root_qbt = state["library_root_qbt"]
	mappings = state["mappings"]

	torrent = qbt.get_torrent(download_id)
	files = qbt.get_files(download_id)
	file_names = [item["name"] for item in files]
	old_root = common_root(file_names)
	desired_root_rel = os.path.relpath(target_root_qbt, library_root_qbt).replace("\\", "/")

	# Detect whether setLocation will move files to a different volume
	location_changed = torrent.get("save_path") != library_root_qbt
	log(f"finalize: source_folder_arr={source_folder_arr}")
	log(f"finalize: target_root_arr={target_root_arr}")
	log(f"finalize: library_root_qbt={library_root_qbt}")
	log(f"finalize: save_path={torrent.get('save_path')} location_changed={location_changed}")
	log(f"finalize: old_root={old_root} desired_root_rel={desired_root_rel}")

	if old_root and desired_root_rel and old_root != desired_root_rel:
		qbt.rename_folder(download_id, old_root, desired_root_rel)
		file_names = [
			desired_root_rel + name[len(old_root):] if name == old_root or name.startswith(old_root + "/") else name
			for name in file_names
		]

	for mapping in mappings:
		desired_rel = os.path.relpath(mapping["imported_path_qbt"], library_root_qbt).replace("\\", "/")
		current_rel = match_current_relative(file_names, desired_rel)
		if current_rel != desired_rel:
			qbt.rename_file(download_id, current_rel, desired_rel)
			file_names[file_names.index(current_rel)] = desired_rel

	if location_changed:
		qbt.set_location(download_id, library_root_qbt)

	# Move remaining non-torrent debris from original source folder
	can_move_source_folder = not is_arr_root_path(source_folder_arr)
	if can_move_source_folder:
		move_remaining_source_items(source_folder_arr, target_root_arr)
	else:
		log(f"Skipping unsafe move_remaining from Arr root: {source_folder_arr}")

	# For cross-volume moves, also clean the renamed source folder on the
	# source volume (e.g. /tv6/The Chosen/Season 4 after rename+setLocation).
	# Skip entirely when source is a library root (root-level file source):
	# computing renamed_source_arr from the root would point to a real show
	# folder that has nothing to do with this torrent.
	renamed_source_arr = None
	if location_changed and can_move_source_folder:
		source_root_arr, _ = longest_prefix_match(source_folder_arr, ARR_TO_QBT_PATHS)
		if source_root_arr and desired_root_rel:
			candidate = str(Path(source_root_arr) / desired_root_rel)
			if candidate != source_folder_arr:
				renamed_source_arr = candidate
				if not is_arr_root_path(renamed_source_arr):
					move_remaining_source_items(renamed_source_arr, target_root_arr)
				else:
					log(f"Skipping unsafe move_remaining from Arr root: {renamed_source_arr}")

	qbt.recheck(download_id)
	torrent = qbt.wait_for_recheck(download_id)
	if torrent.get("progress", 0) < 0.9999:
		fail(f"Recheck did not complete successfully for {download_id}; leaving source folder in place")
	qbt.resume(download_id)

	# Remove source folder(s)
	remove_root_level_source_links(source_folder_arr, mappings)
	if can_move_source_folder:
		remove_source_folder(source_folder_arr)
	else:
		log(f"Skipping unsafe source folder removal for Arr root: {source_folder_arr}")
	if renamed_source_arr:
		if not is_arr_root_path(renamed_source_arr):
			remove_source_folder(renamed_source_arr)
		else:
			log(f"Skipping unsafe source folder removal for Arr root: {renamed_source_arr}")
		source_root_arr, _ = longest_prefix_match(source_folder_arr, ARR_TO_QBT_PATHS)
		if source_root_arr:
			remove_empty_parents(renamed_source_arr, source_root_arr)

	clear_state(app_name, download_id)
	log(f"Finalized torrent sync for {download_id}")


def main():
	app_name = detect_app()
	if not app_name:
		fail("Could not detect Sonarr or Radarr environment")

	event = collect_event(app_name)
	download_id = event["download_id"]

	state = load_state(app_name, download_id)
	state = merge_mapping(state, event)

	qbt = QBittorrentClient()
	qbt.login()
	qbt_files = qbt.get_files(download_id)
	if augment_state_from_target_files(state, qbt_files):
		log(f"Recovered additional imported files for {download_id} from target folder")
	save_state(app_name, download_id, state)
	video_count = torrent_video_count(qbt_files)
	if len(state["mappings"]) < video_count:
		log(
			f"Waiting for import completion for {download_id}; "
			f"have {len(state['mappings'])}/{video_count} imported video files"
		)
		return

	finalize_transfer(app_name, qbt, download_id, state)


if __name__ == "__main__":
	main()
