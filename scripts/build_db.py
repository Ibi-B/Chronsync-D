import glob
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

from lxml import etree


DB_PATH = sys.argv[1] if len(sys.argv) > 1 else "public/epg.sqlite3"
BATCH_SIZE = 5000


class BuildError(Exception):
    pass


def fail(message: str) -> None:
    print(f"EPG build failed: {message}", file=sys.stderr)
    raise BuildError(message)


def parse_xmltv_time(value: str):
    if not value:
        return None

    parts = value.split()
    raw = parts[0]

    if len(raw) == 12:
        local_time = datetime.strptime(raw, "%Y%m%d%H%M")
    elif len(raw) == 14:
        local_time = datetime.strptime(raw, "%Y%m%d%H%M%S")
    else:
        return None

    if len(parts) > 1:
        offset = parts[1]
        if len(offset) != 5 or offset[0] not in "+-" or not offset[1:].isdigit():
            return None

        sign = 1 if offset.startswith("+") else -1
        hours = int(offset[1:3])
        minutes = int(offset[3:5])
        if hours > 23 or minutes > 59:
            return None

        local_time = local_time.replace(
            tzinfo=timezone(sign * timedelta(hours=hours, minutes=minutes))
        )
    else:
        local_time = local_time.replace(tzinfo=timezone.utc)

    return local_time.astimezone(timezone.utc)


def init_database(database_path: str):
    os.makedirs(os.path.dirname(database_path), exist_ok=True)

    if os.path.exists(database_path):
        os.remove(database_path)

    conn = sqlite3.connect(database_path)
    conn.execute("PRAGMA page_size = 4096")
    conn.execute("PRAGMA journal_mode = OFF")
    conn.execute("PRAGMA synchronous = OFF")

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS channels (
            id TEXT PRIMARY KEY,
            name TEXT,
            logo TEXT
        );

        CREATE TABLE IF NOT EXISTS programs (
            channel_id TEXT,
            title TEXT,
            desc TEXT,
            start_time TEXT,
            stop_time TEXT,
            start_utc TEXT,
            stop_utc TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_programs_channel
            ON programs(channel_id);

        CREATE INDEX IF NOT EXISTS idx_programs_time
            ON programs(start_utc, stop_utc);

        CREATE INDEX IF NOT EXISTS idx_programs_channel_time
            ON programs(channel_id, start_utc, stop_utc);
    """)

    return conn


def flush(conn, channels, programs):
    if channels:
        conn.executemany(
            "INSERT OR REPLACE INTO channels (id, name, logo) VALUES (?, ?, ?)",
            channels
        )

    if programs:
        conn.executemany(
            """INSERT INTO programs
            (channel_id, title, desc, start_time, stop_time, start_utc, stop_utc)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            programs
        )

    conn.commit()
    channels.clear()
    programs.clear()


def parse_xml_to_db(
    conn,
    xml_file: str,
    source_counts: dict,
    seen_channel_ids: set,
):
    channels = []
    programs = []
    channel_count = 0
    programme_count = 0
    duplicate_channels = 0

    context = etree.iterparse(
        xml_file,
        events=("end",),
        tag=("channel", "programme")
    )

    for _, elem in context:
        if elem.tag == "channel":
            channel_id = (elem.get("id") or "").strip()
            if not channel_id:
                fail(f"{xml_file}: a channel entry has no usable id")

            if channel_id in seen_channel_ids:
                duplicate_channels += 1

            seen_channel_ids.add(channel_id)

            name_elem = elem.find("display-name")
            logo_elem = elem.find("icon")

            channels.append((
                channel_id,
                name_elem.text if name_elem is not None else "Unknown",
                logo_elem.get("src") if logo_elem is not None else ""
            ))
            channel_count += 1

        elif elem.tag == "programme":
            channel_id = (elem.get("channel") or "").strip()
            start_raw = (elem.get("start") or "").strip()
            stop_raw = (elem.get("stop") or "").strip()

            if not channel_id:
                fail(f"{xml_file}: a programme entry has no channel attribute")

            start_time = parse_xmltv_time(start_raw)
            stop_time = parse_xmltv_time(stop_raw)

            if start_time is None:
                fail(
                    f"{xml_file}: invalid start timestamp for channel "
                    f"{channel_id!r}: {start_raw!r}"
                )

            if stop_time is None:
                fail(
                    f"{xml_file}: invalid stop timestamp for channel "
                    f"{channel_id!r}: {stop_raw!r}"
                )

            if stop_time <= start_time:
                fail(
                    f"{xml_file}: programme stop time is not later than start time "
                    f"for channel {channel_id!r}: "
                    f"start={start_raw!r}, stop={stop_raw!r}"
                )

            title_elem = elem.find("title")
            desc_elem = elem.find("desc")

            programs.append((
                channel_id,
                title_elem.text if title_elem is not None else "Unknown",
                desc_elem.text if desc_elem is not None else "",
                start_raw,
                stop_raw,
                start_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                stop_time.strftime("%Y-%m-%dT%H:%M:%SZ")
            ))
            programme_count += 1

        elem.clear()

        parent = elem.getparent()
        if parent is not None:
            while elem.getprevious() is not None:
                del parent[0]

        if len(channels) >= BATCH_SIZE or len(programs) >= BATCH_SIZE:
            flush(conn, channels, programs)

    flush(conn, channels, programs)

    source_counts[xml_file] = {
        "channels": channel_count,
        "programmes": programme_count,
        "duplicate_channels": duplicate_channels
    }


def verify_database(conn, database_path: str):
    integrity = conn.execute("PRAGMA integrity_check").fetchall()
    if integrity != [("ok",)]:
        fail(f"{database_path}: SQLite integrity check failed: {integrity}")

    channel_count = conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0]
    programme_count = conn.execute("SELECT COUNT(*) FROM programs").fetchone()[0]

    if channel_count == 0:
        fail(f"{database_path}: no channels were imported")

    if programme_count == 0:
        fail(f"{database_path}: no programmes were imported")

    invalid_rows = conn.execute("""
        SELECT COUNT(*)
        FROM programs
        WHERE channel_id IS NULL
           OR TRIM(channel_id) = ''
           OR title IS NULL
           OR start_utc IS NULL
           OR TRIM(start_utc) = ''
           OR stop_utc IS NULL
           OR TRIM(stop_utc) = ''
    """).fetchone()[0]

    if invalid_rows:
        fail(f"{database_path}: {invalid_rows} programme rows have missing required fields")

    invalid_intervals = conn.execute("""
        SELECT COUNT(*)
        FROM programs
        WHERE stop_utc <= start_utc
    """).fetchone()[0]

    if invalid_intervals:
        fail(f"{database_path}: {invalid_intervals} programme rows have invalid UTC intervals")

    orphan_count = conn.execute("""
        SELECT COUNT(*)
        FROM programs p
        LEFT JOIN channels c ON c.id = p.channel_id
        WHERE c.id IS NULL
    """).fetchone()[0]

    duplicate_programmes = conn.execute("""
        SELECT COUNT(*)
        FROM (
            SELECT channel_id, start_utc, stop_utc, title
            FROM programs
            GROUP BY channel_id, start_utc, stop_utc, title
            HAVING COUNT(*) > 1
        )
    """).fetchone()[0]

    time_range = conn.execute("""
        SELECT MIN(start_utc), MAX(stop_utc)
        FROM programs
    """).fetchone()

    print(
        f"Database verification passed: {channel_count} channels, "
        f"{programme_count} programmes, "
        f"coverage {time_range[0]} to {time_range[1]}"
    )
    print(
        f"Diagnostics: orphan_programmes={orphan_count}, "
        f"duplicate_programmes={duplicate_programmes}"
    )


def main():
    xml_files = sorted(glob.glob("raw_data/*.xml"))

    if not xml_files:
        fail("no XML input files were found in raw_data/")

    temporary_database = f"{DB_PATH}.tmp"

    if os.path.exists(temporary_database):
        os.remove(temporary_database)

    conn = None

    try:
        conn = init_database(temporary_database)
        source_counts = {}
        seen_channel_ids = set()

        for xml_file in xml_files:
            parse_xml_to_db(
                conn,
                xml_file,
                source_counts,
                seen_channel_ids
            )

        for xml_file in xml_files:
            counts = source_counts[xml_file]
            print(
                f"{xml_file}: channels={counts['channels']}, "
                f"programmes={counts['programmes']}, "
                f"duplicate_channels={counts['duplicate_channels']}"
            )

        conn.execute("VACUUM")
        verify_database(conn, temporary_database)
        conn.close()
        conn = None

        os.replace(temporary_database, DB_PATH)
        print(f"Database built and verified successfully: {DB_PATH}")

    except BuildError:
        if conn is not None:
            conn.close()

        if os.path.exists(temporary_database):
            os.remove(temporary_database)

        raise

    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    try:
        main()
    except BuildError as error:
        sys.exit(1)
