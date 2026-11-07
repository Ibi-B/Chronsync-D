import glob
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

from lxml import etree

DB_PATH = sys.argv[1] if len(sys.argv) > 1 else "public/epg.sqlite3"
BATCH_SIZE = 5000


def parse_xmltv_time(value):
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
        sign = 1 if offset.startswith("+") else -1
        offset = offset.lstrip("+-")
        hours = int(offset[:2])
        minutes = int(offset[2:4])
        local_time = local_time.replace(
            tzinfo=timezone(sign * timedelta(hours=hours, minutes=minutes))
        )
    else:
        local_time = local_time.replace(tzinfo=timezone.utc)

    return local_time.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)

    conn = sqlite3.connect(DB_PATH)
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


def parse_xml_to_db(conn, xml_file):
    channels = []
    programs = []

    context = etree.iterparse(
        xml_file,
        events=("end",),
        tag=("channel", "programme")
    )

    for _, elem in context:
        if elem.tag == "channel":
            name_elem = elem.find("display-name")
            logo_elem = elem.find("icon")

            channels.append((
                elem.get("id"),
                name_elem.text if name_elem is not None else "Unknown",
                logo_elem.get("src") if logo_elem is not None else ""
            ))

        elif elem.tag == "programme":
            title_elem = elem.find("title")
            desc_elem = elem.find("desc")

            programs.append((
                elem.get("channel"),
                title_elem.text if title_elem is not None else "Unknown",
                desc_elem.text if desc_elem is not None else "",
                elem.get("start"),
                elem.get("stop"),
                parse_xmltv_time(elem.get("start")),
                parse_xmltv_time(elem.get("stop"))
            ))

        elem.clear()

        parent = elem.getparent()
        if parent is not None:
            while elem.getprevious() is not None:
                del parent[0]

        if len(channels) >= BATCH_SIZE or len(programs) >= BATCH_SIZE:
            flush(conn, channels, programs)

    flush(conn, channels, programs)


def main():
    conn = init_db()

    try:
        for xml_file in sorted(glob.glob("raw_data/*.xml")):
            parse_xml_to_db(conn, xml_file)

        conn.execute("VACUUM")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
