import importlib.util
import os
import tempfile
import unittest
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


if __name__ == "__main__":
	unittest.main()
