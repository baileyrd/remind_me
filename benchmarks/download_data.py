"""
benchmarks.download_data — Fetch the LongMemEval dataset files.

Downloads the cleaned LongMemEval JSON files from the official HuggingFace
dataset repo (``xiaowu0162/longmemeval-cleaned``) using only the Python standard
library — no ``wget``/``curl`` and no extra dependencies. Each file is validated
as parseable JSON after download.

Usage::

    python -m benchmarks.download_data --dataset oracle      # smallest, start here
    python -m benchmarks.download_data --dataset s           # standard 115k-token haystacks
    python -m benchmarks.download_data --dataset all

Files land in ``benchmarks/data/`` by default (git-ignored).
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

# Official cleaned distribution (see https://github.com/xiaowu0162/LongMemEval).
_BASE_URL = "https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main"

# Short name -> remote filename.
DATASETS: dict[str, str] = {
    "oracle": "longmemeval_oracle.json",
    "s": "longmemeval_s_cleaned.json",
    "m": "longmemeval_m_cleaned.json",
}

# A browser-like User-Agent: HuggingFace sometimes rejects the default urllib UA.
_HEADERS = {"User-Agent": "Mozilla/5.0 (remind-me-benchmarks)"}

_DEFAULT_DEST = Path(__file__).resolve().parent / "data"


def _human(num_bytes: float) -> str:
    """Format a byte count as a human-readable string."""
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024 or unit == "GB":
            return f"{num_bytes:.1f}{unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f}GB"


def _download(url: str, dest: Path) -> None:
    """Stream *url* to *dest*, printing progress to stderr."""
    req = urllib.request.Request(url, headers=_HEADERS)  # noqa: S310 - https URL, fixed host
    with urllib.request.urlopen(req) as resp:  # noqa: S310
        total = int(resp.headers.get("Content-Length", 0))
        tmp = dest.with_suffix(dest.suffix + ".part")
        downloaded = 0
        chunk = 1 << 20  # 1 MiB
        with tmp.open("wb") as fh:
            while True:
                block = resp.read(chunk)
                if not block:
                    break
                fh.write(block)
                downloaded += len(block)
                if total:
                    pct = 100 * downloaded / total
                    print(
                        f"\r  {dest.name}: {_human(downloaded)}/{_human(total)} ({pct:.0f}%)",
                        end="",
                        file=sys.stderr,
                    )
                else:
                    print(f"\r  {dest.name}: {_human(downloaded)}", end="", file=sys.stderr)
        print("", file=sys.stderr)
        tmp.replace(dest)


def _validate_json(path: Path) -> int:
    """Confirm *path* parses as a JSON array of questions; return the count."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        for key in ("data", "questions", "items"):
            if isinstance(raw.get(key), list):
                raw = raw[key]
                break
    if not isinstance(raw, list):
        raise ValueError("downloaded file is not a JSON list of questions")
    return len(raw)


def fetch(name: str, dest_dir: Path, force: bool) -> Path:
    """Download one named dataset into *dest_dir*, returning its path."""
    filename = DATASETS[name]
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / filename

    if dest.exists() and not force:
        print(f"✓ {filename} already present (use --force to re-download)", file=sys.stderr)
        return dest

    url = f"{_BASE_URL}/{filename}"
    print(f"Downloading {filename} from HuggingFace…", file=sys.stderr)
    try:
        _download(url, dest)
    except urllib.error.HTTPError as e:
        raise SystemExit(
            f"error: HTTP {e.code} fetching {url}\n"
            "The dataset layout may have changed — check "
            "https://github.com/xiaowu0162/LongMemEval for current download instructions."
        ) from e
    except urllib.error.URLError as e:
        raise SystemExit(f"error: network failure fetching {url}: {e.reason}") from e

    count = _validate_json(dest)
    print(f"✓ {filename} ready ({count} questions) -> {dest}", file=sys.stderr)
    return dest


def build_parser() -> argparse.ArgumentParser:
    """Construct the CLI parser."""
    p = argparse.ArgumentParser(
        prog="python -m benchmarks.download_data",
        description="Download LongMemEval dataset files for the retrieval benchmark.",
    )
    p.add_argument(
        "--dataset",
        choices=[*DATASETS.keys(), "all"],
        default="oracle",
        help="Which dataset to fetch (default: oracle — smallest, best for a first run)",
    )
    p.add_argument(
        "--dest",
        type=Path,
        default=_DEFAULT_DEST,
        help=f"Destination directory (default: {_DEFAULT_DEST})",
    )
    p.add_argument("--force", action="store_true", help="Re-download even if the file exists")
    return p


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    args = build_parser().parse_args(argv)
    names = list(DATASETS) if args.dataset == "all" else [args.dataset]

    paths = [fetch(name, args.dest, args.force) for name in names]

    print("\nReady. Run the benchmark with, e.g.:", file=sys.stderr)
    print(
        f"  python -m benchmarks.runner --data {paths[0]} "
        "--ingest verbatim,atomic --embedder real --ks 1,3,5,10 --progress --out results.json",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
