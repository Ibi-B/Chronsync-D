import gzip
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
_saved_argv = sys.argv[:]
sys.argv = [str(ROOT / "scripts" / "build_db.py")]
import build_db
import fetch_feeds
import validate_epg
sys.argv = _saved_argv


def programme(start="202611131200 +0000", stop="202611131300 +0000",
              title="News", channel="news"):
    channel_attr = "" if channel is None else f' channel="{channel}"'
    title_xml = "" if title is None else f"<title>{title}</title>"
    return (
        f'<programme{channel_attr} start="{start}" stop="{stop}">'
        f"{title_xml}</programme>"
    )


def write_feed(path, programmes, root="tv", include_channel=True):
    channel_xml = (
        '<channel id="news"><display-name>News</display-name></channel>'
        if include_channel else ""
    )
    path.write_text(
        f"<{root}>{channel_xml}{''.join(programmes)}</{root}>",
        encoding="utf-8",
    )
    return path


class EpgPipelineTests(unittest.TestCase):
    def test_validator_skips_bad_records_but_keeps_valid_feed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mixed.xml"
            write_feed(path, [
                programme(),
                programme(start="202602301200", stop="202603011300"),
                programme(start="202611131200", stop="202611131200"),
                programme(title="   "),
                programme(channel=None),
            ])
            validate_epg.validate(path)

    def test_validator_rejects_non_xmltv_root(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wrong-root.xml"
            write_feed(path, [programme()], root="xml")
            with self.assertRaises(SystemExit):
                validate_epg.validate(path)

    def test_builder_skips_invalid_records_and_preserves_required_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mixed.xml"
            database_path = Path(directory) / "fixture.sqlite3"
            write_feed(path, [
                programme(),
                programme(start="202602301200", stop="202603011300"),
                programme(title="   "),
                programme(start="202611131200", stop="202611131200"),
            ])
            connection = build_db.init_database(str(database_path))
            counts = {}
            build_db.parse_xml_to_db(connection, str(path), counts, set())
            build_db.verify_database(connection, str(database_path))
            self.assertEqual(counts[str(path)]["programmes"], 1)
            self.assertEqual(counts[str(path)]["invalid_programmes"], 3)
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(programs)")
            }
            self.assertEqual(
                columns,
                {"channel_id", "title", "desc", "start_utc", "stop_utc"},
            )
            indexes = {
                row[1] for row in connection.execute("PRAGMA index_list(programs)")
            }
            self.assertEqual(indexes, {"idx_programs_time"})
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM programs").fetchone()[0],
                1,
            )
            connection.close()

    def test_builder_checks_root_and_keeps_orphans_reportable(self):
        with tempfile.TemporaryDirectory() as directory:
            wrong_root = Path(directory) / "wrong.xml"
            write_feed(wrong_root, [programme()], root="xml")
            database_path = Path(directory) / "fixture.sqlite3"
            connection = build_db.init_database(str(database_path))
            with self.assertRaises(build_db.BuildError):
                build_db.parse_xml_to_db(connection, str(wrong_root), {}, set())
            connection.close()

            valid = Path(directory) / "orphan.xml"
            write_feed(valid, [programme(channel="not-in-feed")])
            connection = build_db.init_database(str(database_path))
            counts = {}
            build_db.parse_xml_to_db(connection, str(valid), counts, set())
            build_db.verify_database(connection, str(database_path))
            orphan_count = connection.execute(
                "SELECT COUNT(*) FROM programs p "
                "LEFT JOIN channels c ON c.id=p.channel_id WHERE c.id IS NULL"
            ).fetchone()[0]
            self.assertEqual(orphan_count, 1)
            connection.close()

    def test_coverage_baseline_warns_on_decline_and_fails_major_collapse(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "coverage.sqlite3"
            connection = build_db.init_database(str(database_path))
            connection.execute(
                "INSERT INTO channels (id, name, logo) VALUES (?, ?, ?)",
                ("news", "News", ""),
            )
            connection.execute(
                "INSERT INTO programs "
                "(channel_id, title, desc, start_utc, stop_utc) "
                "VALUES (?, ?, ?, ?, ?)",
                ("news", "News", "", "2030-01-01T12:00:00Z", "2030-01-01T13:00:00Z"),
            )
            connection.commit()

            baseline = {
                "baseline_date": "2026-11-10",
                "channels": 2,
                "programmes": 2,
                "minimum_ratio": 0.5,
            }
            build_db.verify_coverage(connection, baseline)
            baseline["channels"] = 3
            baseline["programmes"] = 3
            with self.assertRaises(build_db.BuildError):
                build_db.verify_coverage(connection, baseline)
            connection.close()

    def test_large_stream_flush_keeps_all_programmes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "large.xml"
            database_path = Path(directory) / "large.sqlite3"
            with path.open("w", encoding="utf-8") as output:
                output.write('<tv><channel id="news"><display-name>News</display-name></channel>')
                for index in range(5100):
                    output.write(programme(
                        start="202611131200 +0000",
                        stop="202611131300 +0000",
                        title=f"Programme {index}",
                    ))
                output.write("</tv>")
            connection = build_db.init_database(str(database_path))
            counts = {}
            build_db.parse_xml_to_db(connection, str(path), counts, set())
            self.assertEqual(counts[str(path)]["programmes"], 5100)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM programs").fetchone()[0],
                5100,
            )
            connection.close()

    def test_fetcher_rejects_only_invalid_feed_and_removes_files(self):
        with tempfile.TemporaryDirectory() as directory:
            compressed = Path(directory) / "feed.xml.gz"
            output = Path(directory) / "feed.xml"
            xml = (
                '<tv><channel id="news"><display-name>News</display-name></channel>'
                + programme() + "</tv>"
            ).encode()

            def fake_run(command, check=True, stdout=None):
                if command[0] == "curl":
                    destination = command[command.index("-o") + 1]
                    with gzip.open(destination, "wb") as archive:
                        archive.write(xml)
                elif command[0] == "gzip":
                    with gzip.open(command[-1], "rb") as archive:
                        archive.read(1)
                elif command[0] == "gunzip":
                    with gzip.open(command[-1], "rb") as archive:
                        shutil.copyfileobj(archive, stdout)

            with mock.patch.object(fetch_feeds.subprocess, "run", side_effect=fake_run), \
                 mock.patch.object(fetch_feeds, "validate", side_effect=SystemExit(1)):
                result = fetch_feeds.fetch_feed(
                    "https://example.invalid/feed.xml.gz",
                    str(compressed),
                    str(output),
                    retries=1,
                    timeout=5,
                )

            self.assertEqual(result[0], False)
            self.assertEqual(result[1], "skipped_invalid_xmltv")
            self.assertFalse(compressed.exists())
            self.assertFalse(output.exists())

    def test_fetcher_continues_after_bad_source_and_fails_if_none_are_usable(self):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "source_report.tsv"
            arguments = [
                "fetch_feeds.py", "--report", str(report),
                "--feed", "https://one.invalid/a.gz", str(Path(directory) / "a.xml"),
                "--feed", "https://two.invalid/b.gz", str(Path(directory) / "b.xml"),
            ]
            with mock.patch.object(sys, "argv", arguments), \
                 mock.patch.object(fetch_feeds, "fetch_feed", side_effect=[
                     (False, "skipped_invalid_xmltv", 1, 2),
                     (True, "ok", 1, 1),
                 ]) as fetch:
                self.assertEqual(fetch_feeds.main(), 0)
                self.assertEqual(fetch.call_count, 2)

            with mock.patch.object(sys, "argv", arguments), \
                 mock.patch.object(fetch_feeds, "fetch_feed", return_value=(
                     False, "failed: unavailable", 0, 0
                 )):
                self.assertEqual(fetch_feeds.main(), 1)


if __name__ == "__main__":
    unittest.main()
