import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from lxml import etree


TIMESTAMP_PATTERN = re.compile(r"^(\d{12}|\d{14})(?:\s+([+-]\d{4}))?$")


class RecordDefect(Exception):
    pass


def fail(message: str) -> None:
    print(f"EPG validation failed: {message}", file=sys.stderr)
    raise SystemExit(1)


def parse_xmltv_timestamp(value: str, field: str) -> tuple[datetime, datetime]:
    match = TIMESTAMP_PATTERN.match(value.strip())
    if not match:
        raise RecordDefect(f"invalid XMLTV timestamp in {field}: {value!r}")

    raw, offset = match.groups()
    time_format = "%Y%m%d%H%M" if len(raw) == 12 else "%Y%m%d%H%M%S"

    try:
        local_time = datetime.strptime(raw, time_format)
    except ValueError as error:
        raise RecordDefect(
            f"invalid calendar date or time in {field}: {value!r}: {error}"
        )

    if offset:
        sign = 1 if offset.startswith("+") else -1
        digits = offset.lstrip("+-")
        hours = int(digits[:2])
        minutes = int(digits[2:4])
        if hours > 23 or minutes > 59:
            raise RecordDefect(f"invalid timezone offset in {field}: {value!r}")
        local_time = local_time.replace(
            tzinfo=timezone(sign * timedelta(hours=hours, minutes=minutes))
        )
    else:
        local_time = local_time.replace(tzinfo=timezone.utc)

    return local_time, local_time.astimezone(timezone.utc)


def validate(path: Path) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        fail(f"{path}: EPG file is missing or empty")

    channel_count = 0
    programme_count = 0
    invalid_programme_count = 0

    try:
        context = etree.iterparse(
            str(path),
            events=("end",),
            tag=("channel", "programme"),
        )

        for _, element in context:
            if element.tag == "channel":
                if not (element.get("id") or "").strip():
                    fail(f"{path}: a channel entry has no usable id")
                channel_count += 1

            elif element.tag == "programme":
                channel_ref = (element.get("channel") or "").strip()
                start = (element.get("start") or "").strip()
                stop = (element.get("stop") or "").strip()

                try:
                    if not channel_ref:
                        raise RecordDefect(
                            "programme entry has no channel attribute"
                        )

                    if not start or not stop:
                        raise RecordDefect(
                            "programme entry is missing start or stop"
                        )

                    _, start_utc = parse_xmltv_timestamp(start, "start")
                    _, stop_utc = parse_xmltv_timestamp(stop, "stop")

                    if stop_utc <= start_utc:
                        raise RecordDefect(
                            "programme stop time is not later than start time: "
                            f"channel={channel_ref!r}, "
                            f"start={start!r}, stop={stop!r}"
                        )

                    programme_count += 1

                except RecordDefect as error:
                    invalid_programme_count += 1
                    print(
                        f"{path}: skipped invalid programme record: {error}",
                        file=sys.stderr,
                    )

            element.clear()
            parent = element.getparent()
            if parent is not None:
                while element.getprevious() is not None:
                    del parent[0]

    except etree.XMLSyntaxError as error:
        fail(f"{path}: file is not well-formed XML: {error}")

    if channel_count == 0:
        fail(f"{path}: no <channel> elements found")

    if programme_count == 0:
        fail(
            f"{path}: no valid <programme> elements found; "
            f"invalid_records={invalid_programme_count}"
        )

    print(
        f"{path}: EPG validation passed: "
        f"{channel_count} channels, {programme_count} valid programmes, "
        f"{invalid_programme_count} invalid records skipped"
    )


if __name__ == "__main__":
    if len(sys.argv) != 2:
        fail("usage: validate_epg.py <xml-file>")

    validate(Path(sys.argv[1]))
