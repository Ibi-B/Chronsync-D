import argparse
import gzip
import os
import subprocess
import sys
import time

from lxml import etree
from pathlib import Path

from validate_epg import validate


def count_xmltv_entries(path):
    channel_count = 0
    programme_count = 0

    context = etree.iterparse(
        path,
        events=("end",),
        tag=("channel", "programme")
    )

    for _, elem in context:
        if elem.tag == "channel":
            channel_count += 1
        elif elem.tag == "programme":
            programme_count += 1

        elem.clear()

        parent = elem.getparent()
        if parent is not None:
            while elem.getprevious() is not None:
                del parent[0]

    return channel_count, programme_count


def remove_files(*paths):
    for path in paths:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


def fetch_feed(url, compressed_path, output_path, retries, timeout):
    last_error = "unknown error"
    last_byte_count = 0

    for attempt in range(1, retries + 1):
        byte_count = 0
        try:
            subprocess.run(
                [
                    "curl",
                    "--fail",
                    "--location",
                    "--show-error",
                    "--silent",
                    "--max-time",
                    str(timeout),
                    url,
                    "-o",
                    compressed_path,
                ],
                check=True,
            )

            if not os.path.exists(compressed_path) or os.path.getsize(compressed_path) == 0:
                raise RuntimeError("downloaded gzip file is empty")

            subprocess.run(
                ["gzip", "-t", compressed_path],
                check=True,
            )

            with open(output_path, "wb") as output_file:
                subprocess.run(
                    ["gunzip", "-c", compressed_path],
                    stdout=output_file,
                    check=True,
                )

            if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
                raise RuntimeError("extracted XML file is empty")

            byte_count = os.path.getsize(output_path)
            channel_count, programme_count = count_xmltv_entries(output_path)

            if programme_count == 0:
                remove_files(compressed_path, output_path)
                return False, "skipped_zero_programmes", channel_count, programme_count, byte_count

            try:
                validate(Path(output_path))
            except SystemExit:
                # Reject this feed only; the caller continues with other sources.
                remove_files(compressed_path, output_path)
                return False, "skipped_invalid_xmltv", channel_count, programme_count, byte_count

            return True, "ok", channel_count, programme_count, byte_count

        except Exception as error:
            last_error = str(error)
            if os.path.exists(output_path):
                byte_count = os.path.getsize(output_path)
            last_byte_count = byte_count
            remove_files(compressed_path, output_path)

            if attempt < retries:
                time.sleep(5 * attempt)

    return False, f"failed: {last_error}", 0, 0, last_byte_count


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", required=True)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--feed", action="append", nargs=2, default=[])
    args = parser.parse_args()

    if not args.feed:
        print("No feeds were configured", file=sys.stderr)
        return 1

    os.makedirs(os.path.dirname(args.report), exist_ok=True)

    successful = 0
    failed = 0
    skipped = 0

    with open(args.report, "w", encoding="utf-8") as report:
        report.write("source\turl\tbytes\tstatus\tchannels\tprogrammes\n")

        for url, output_path in args.feed:
            compressed_path = f"{output_path}.gz"
            ok, status, channel_count, programme_count, byte_count = fetch_feed(
                url,
                compressed_path,
                output_path,
                args.retries,
                args.timeout,
            )

            report.write(
                f"{os.path.basename(output_path)}\t{url}\t{byte_count}\t"
                f"{status}\t{channel_count}\t{programme_count}\n"
            )

            if ok:
                successful += 1
            elif status.startswith("skipped"):
                skipped += 1
            else:
                failed += 1

    print(
        f"Feed acquisition complete: successful={successful}, "
        f"skipped={skipped}, failed={failed}"
    )

    if successful == 0:
        print("No usable feeds were acquired", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
