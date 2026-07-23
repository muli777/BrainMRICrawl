"""Split preprocessed JSONL data into two separate JSON files:
   - content.json: Only abstract and text_content
   - metadata.json: All other metadata fields

Usage:
    python split_rag_data.py --input DataClean/preprocessed_no_filter/preprocessed_articles.jsonl
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def split_data(input_file: Path, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    content_list = []
    metadata_list = []

    content_file = output_dir / "content.json"
    metadata_file = output_dir / "metadata.json"

    with input_file.open("r", encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            try:
                article = json.loads(line)
            except json.JSONDecodeError as exc:
                print(f"Skipping line {i}: JSON decode error - {exc}")
                continue

            content = {
                "id": article.get("pmcid") or article.get("pprid") or f"article_{i}",
                "title": article.get("title", ""),
                "abstract": article.get("abstract", ""),
                "text_content": article.get("text_content", "")
            }
            content_list.append(content)

            metadata = {}
            for key, value in article.items():
                if key not in ("abstract", "text_content"):
                    metadata[key] = value
            metadata_list.append(metadata)

            if i % 200 == 0:
                print(f"Processed {i} articles")

    with content_file.open("w", encoding="utf-8") as f:
        json.dump(content_list, f, ensure_ascii=False, indent=2)

    with metadata_file.open("w", encoding="utf-8") as f:
        json.dump(metadata_list, f, ensure_ascii=False, indent=2)

    print(f"\n=== Split Complete ===")
    print(f"Total articles: {len(content_list)}")
    print(f"Content file: {content_file}")
    print(f"Metadata file: {metadata_file}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Split RAG data into content and metadata")
    parser.add_argument(
        "--input",
        type=str,
        default="DataClean/preprocessed_no_filter/preprocessed_articles.jsonl",
        help="Input JSONL file"
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="DataClean/split_data",
        help="Output directory"
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output_dir)

    if not input_path.exists():
        print(f"Error: Input file '{input_path}' does not exist")
        return

    split_data(input_path, output_path)


if __name__ == "__main__":
    main()