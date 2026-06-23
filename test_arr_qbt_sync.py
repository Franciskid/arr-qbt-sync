import importlib.util
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock


PROJECT_DIR = Path(__file__).resolve().parent
os.environ["ARR_QBT_SYNC_CONFIG"] = str(PROJECT_DIR / "config.json")
os.environ["ARR_QBT_SYNC_STATE_DIR"] = tempfile.mkdtemp()

spec = importlib.util.spec_from_file_location(
	"arr_qbt_sync", PROJECT_DIR / "arr_qbt_sync.py"
)
arr_qbt_sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(arr_qbt_sync)


class PathMappingTests(unittest.TestCase):
	def test_longest_mapping_is_used(self):
		self.assertEqual(
			arr_qbt_sync.arr_to_qbt("/movies6/Smile (2022)/movie.mkv"),
			"/media6/Movies/Smile (2022)/movie.mkv",
		)

	def test_jellyfin_mapping_is_used(self):
		self.assertEqual(
			arr_qbt_sync.arr_to_jellyfin("/tv6/Clarkson's Farm"),
			"/media6:ro/TV Shows/Clarkson's Farm",
		)

	def test_video_extensions_come_from_config(self):
		self.assertTrue(arr_qbt_sync.is_video_file("movie.MKV"))
		self.assertFalse(arr_qbt_sync.is_video_file("poster.jpg"))


class RootLinkCleanupTests(unittest.TestCase):
	def test_removes_only_same_inode_root_link(self):
		with tempfile.TemporaryDirectory() as temp_dir:
			root = Path(temp_dir)
			source = root / "movie.mkv"
			imported = root / "Movie (2026)" / "movie.mkv"
			imported.parent.mkdir()
			source.write_bytes(b"movie")
			os.link(source, imported)

			with mock.patch.object(
				arr_qbt_sync, "is_arr_root_path", side_effect=lambda path: Path(path) == root
			):
				arr_qbt_sync.remove_root_level_source_links(
					str(root),
					[{
						"source_path_arr": str(source),
						"imported_path_arr": str(imported),
					}],
				)

			self.assertFalse(source.exists())
			self.assertEqual(imported.read_bytes(), b"movie")

	def test_keeps_different_files(self):
		with tempfile.TemporaryDirectory() as temp_dir:
			root = Path(temp_dir)
			source = root / "movie.mkv"
			imported = root / "Movie (2026)" / "movie.mkv"
			imported.parent.mkdir()
			source.write_bytes(b"source")
			imported.write_bytes(b"imported")

			with mock.patch.object(
				arr_qbt_sync, "is_arr_root_path", side_effect=lambda path: Path(path) == root
			):
				arr_qbt_sync.remove_root_level_source_links(
					str(root),
					[{
						"source_path_arr": str(source),
						"imported_path_arr": str(imported),
					}],
				)

			self.assertTrue(source.exists())


class SourceCleanupGuardTests(unittest.TestCase):
	def test_move_remaining_blocks_conflicting_video_destination(self):
		with tempfile.TemporaryDirectory() as temp_dir:
			root = Path(temp_dir)
			source = root / "source"
			target = root / "target"
			source.mkdir()
			target.mkdir()
			(source / "movie.mkv").write_bytes(b"source")
			(target / "movie.mkv").write_bytes(b"target")

			result = arr_qbt_sync.move_remaining_source_items(str(source), str(target))

			self.assertFalse(result)
			self.assertEqual((source / "movie.mkv").read_bytes(), b"source")
			self.assertEqual((target / "movie.mkv").read_bytes(), b"target")

	def test_remove_source_folder_refuses_unique_video(self):
		with tempfile.TemporaryDirectory() as temp_dir:
			root = Path(temp_dir)
			source = root / "release"
			source.mkdir()
			(source / "movie.mkv").write_bytes(b"source")

			result = arr_qbt_sync.remove_source_folder(str(source), [])

			self.assertFalse(result)
			self.assertTrue(source.exists())
			self.assertTrue((source / "movie.mkv").exists())

	def test_remove_source_folder_allows_samefile_imported_video(self):
		with tempfile.TemporaryDirectory() as temp_dir:
			root = Path(temp_dir)
			source = root / "release"
			target = root / "Movie (2026)"
			source.mkdir()
			target.mkdir()
			source_file = source / "movie.mkv"
			target_file = target / "movie.mkv"
			source_file.write_bytes(b"movie")
			os.link(source_file, target_file)

			result = arr_qbt_sync.remove_source_folder(
				str(source),
				[{
					"source_path_arr": str(source_file),
					"imported_path_arr": str(target_file),
				}],
			)

			self.assertTrue(result)
			self.assertFalse(source.exists())
			self.assertEqual(target_file.read_bytes(), b"movie")

	def test_cleanup_finished_requires_all_source_folders_gone(self):
		with tempfile.TemporaryDirectory() as temp_dir:
			root = Path(temp_dir)
			source = root / "source"
			renamed = root / "renamed"
			source.mkdir()

			self.assertFalse(arr_qbt_sync.cleanup_finished(str(source), str(renamed)))
			source.rmdir()
			self.assertTrue(arr_qbt_sync.cleanup_finished(str(source), str(renamed)))


class RecheckTests(unittest.TestCase):
	@mock.patch.object(arr_qbt_sync.time, "sleep")
	def test_waits_for_delayed_check_to_finish(self, _sleep):
		client = object.__new__(arr_qbt_sync.QBittorrentClient)
		states = iter([
			{"state": "stalledUP"},
			{"state": "queuedForChecking"},
			{"state": "checkingUP"},
			{"state": "stalledUP", "progress": 1},
		])
		client.get_torrent = lambda _download_id: next(states)

		result = client.wait_for_recheck("hash", timeout_seconds=20)

		self.assertEqual(result["progress"], 1)


class RenameResilienceTests(unittest.TestCase):
	def _client(self):
		return object.__new__(arr_qbt_sync.QBittorrentClient)

	def test_rename_folder_returns_true_on_success(self):
		client = self._client()
		client._request = mock.Mock(return_value=b"")
		self.assertTrue(client.rename_folder("hash", "old", "new"))

	def test_rename_folder_soft_fails_on_conflict(self):
		client = self._client()
		client._request = mock.Mock(
			side_effect=urllib.error.HTTPError("url", 409, "Conflict", {}, None)
		)
		# Must not raise: a 409 (destination already in use) means Arr already
		# placed the file, so finalize should be able to skip cleanup safely.
		self.assertFalse(client.rename_folder("hash", "old", "new"))

	def test_rename_file_soft_fails_on_conflict(self):
		client = self._client()
		client._request = mock.Mock(
			side_effect=urllib.error.HTTPError("url", 409, "Conflict", {}, None)
		)
		self.assertFalse(client.rename_file("hash", "old", "new"))


class FinalizeRepointTests(unittest.TestCase):
	def _state(self):
		return {
			"source_folder_arr": "/movies6/Movie.RELEASE",
			"target_root_arr": "/movies6/Movie (2026)",
			"target_root_qbt": "/media6/Movies/Movie (2026)",
			"library_root_qbt": "/media6/Movies",
			"library_item_root_arr": "/movies6/Movie (2026)",
			"mappings": [
				arr_qbt_sync.build_mapping(
					"/movies6/Movie.RELEASE/Movie.RELEASE.mkv",
					"/movies6/Movie (2026)/Movie.RELEASE.mkv",
				)
			],
		}

	def test_failed_renamefolder_skips_source_removal(self):
		qbt = mock.Mock()
		qbt.get_torrent.return_value = {"save_path": "/media6/Movies", "state": "stalledUP", "progress": 1}
		qbt.get_files.return_value = [
			{"name": "Movie.RELEASE/Movie.RELEASE.mkv"},
			{"name": "Movie.RELEASE/poster.jpg"},
		]
		qbt.rename_folder.return_value = False  # qBittorrent rejects the rename
		qbt.wait_for_recheck.return_value = {"progress": 1}

		with mock.patch.object(arr_qbt_sync, "remove_source_folder") as remove_source, \
			mock.patch.object(arr_qbt_sync, "move_remaining_source_items") as move_remaining, \
			mock.patch.object(arr_qbt_sync, "clear_state") as clear_state:
			arr_qbt_sync.finalize_transfer("radarr", qbt, "hash", self._state())

		# The whole point of the fix: never delete the source folder or wipe
		# state when qBittorrent kept its original paths.
		remove_source.assert_not_called()
		move_remaining.assert_not_called()
		clear_state.assert_not_called()
		qbt.recheck.assert_called_once()

	def test_successful_finalize_triggers_jellyfin_scan_after_clearing_state(self):
		qbt = mock.Mock()
		qbt.get_torrent.return_value = {"save_path": "/media6/Movies", "state": "stalledUP", "progress": 1}
		qbt.get_files.return_value = [
			{"name": "Movie.RELEASE/Movie.RELEASE.mkv"},
			{"name": "Movie.RELEASE/poster.jpg"},
		]
		qbt.rename_folder.return_value = True
		qbt.wait_for_recheck.return_value = {"progress": 1}

		with mock.patch.object(arr_qbt_sync, "move_remaining_source_items", return_value=True), \
			mock.patch.object(arr_qbt_sync, "remove_source_folder", return_value=True), \
			mock.patch.object(arr_qbt_sync, "imported_videos_are_present", return_value=True), \
			mock.patch.object(arr_qbt_sync, "clear_state") as clear_state, \
			mock.patch.object(arr_qbt_sync, "trigger_jellyfin_scan") as trigger_scan:
			arr_qbt_sync.finalize_transfer("radarr", qbt, "hash", self._state())

		clear_state.assert_called_once_with("radarr", "hash")
		trigger_scan.assert_called_once_with("/movies6/Movie (2026)")

	def test_cleanup_warning_does_not_preserve_state_when_source_is_gone(self):
		qbt = mock.Mock()
		qbt.get_torrent.return_value = {"save_path": "/media6/Movies", "state": "stalledUP", "progress": 1}
		qbt.get_files.return_value = [
			{"name": "Movie.RELEASE/Movie.RELEASE.mkv"},
			{"name": "Movie.RELEASE/poster.jpg"},
		]
		qbt.rename_folder.return_value = True
		qbt.wait_for_recheck.return_value = {"progress": 1}

		with mock.patch.object(arr_qbt_sync, "move_remaining_source_items", return_value=False), \
			mock.patch.object(arr_qbt_sync, "remove_source_folder", return_value=True), \
			mock.patch.object(arr_qbt_sync, "cleanup_finished", return_value=True), \
			mock.patch.object(arr_qbt_sync, "imported_videos_are_present", return_value=True), \
			mock.patch.object(arr_qbt_sync, "clear_state") as clear_state, \
			mock.patch.object(arr_qbt_sync, "trigger_jellyfin_scan") as trigger_scan:
			arr_qbt_sync.finalize_transfer("radarr", qbt, "hash", self._state())

		clear_state.assert_called_once_with("radarr", "hash")
		trigger_scan.assert_called_once_with("/movies6/Movie (2026)")


if __name__ == "__main__":
	unittest.main()
