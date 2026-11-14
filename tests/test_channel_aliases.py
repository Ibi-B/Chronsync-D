import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import build_db


class ChannelAliasDatabaseTests(unittest.TestCase):
    def test_all_xmltv_display_names_are_published_as_aliases(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = os.path.join(temporary_directory, "epg.sqlite3")
            first_xml = os.path.join(temporary_directory, "first.xml")
            second_xml = os.path.join(temporary_directory, "second.xml")
            Path(first_xml).write_text(
                '<tv><channel id="news.one">'
                '<display-name lang="en">News One</display-name>'
                '<display-name lang="en">News International</display-name>'
                '<display-name lang="fr">Nouvelles</display-name>'
                '</channel></tv>',
                encoding="utf-8",
            )
            Path(second_xml).write_text(
                '<tv><channel id="news.one">'
                '<display-name lang="en">News One HD</display-name>'
                '</channel></tv>',
                encoding="utf-8",
            )

            connection = build_db.init_database(database_path)
            try:
                source_counts = {}
                seen_channel_ids = set()
                build_db.parse_xml_to_db(
                    connection, first_xml, source_counts, seen_channel_ids
                )
                build_db.parse_xml_to_db(
                    connection, second_xml, source_counts, seen_channel_ids
                )

                channel = connection.execute(
                    "SELECT name FROM channels WHERE id = ?",
                    ("news.one",),
                ).fetchone()
                aliases = {
                    row[0]
                    for row in connection.execute(
                        "SELECT alias FROM channel_aliases WHERE channel_id = ?",
                        ("news.one",),
                    )
                }
                language_by_alias = dict(
                    connection.execute(
                        "SELECT alias, language FROM channel_aliases "
                        "WHERE channel_id = ?",
                        ("news.one",),
                    ).fetchall()
                )

                self.assertEqual(channel, ("News One HD",))
                self.assertEqual(
                    aliases,
                    {"News One", "News International", "Nouvelles", "News One HD"},
                )
                self.assertEqual(language_by_alias["Nouvelles"], "fr")
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
