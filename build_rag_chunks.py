"""Convert Europe PMC JATS XML files into source-traceable RAG chunks."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Iterator


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def node_text(node: ET.Element) -> str:
    return normalize(" ".join(node.itertext()))


def article_metadata(root: ET.Element) -> dict[str, str]:
    def find_text(path: str) -> str:
        return node_text(root.find(path)) if root.find(path) is not None else ""

    doi = ""
    pmcid = ""
    for article_id in root.findall(".//article-meta/article-id"):
        if article_id.attrib.get("pub-id-type") == "doi":
            doi = node_text(article_id)
        if article_id.attrib.get("pub-id-type") == "pmc":
            pmcid = node_text(article_id)
    return {"title": find_text(".//article-title"), "doi": doi, "pmcid": pmcid}


def sections(node: ET.Element, parents: list[str] | None = None) -> Iterator[tuple[str, str]]:
    parents = parents or []
    for sec in node.findall("./sec"):
        title_node = sec.find("title")
        title = node_text(title_node) if title_node is not None else "Untitled section"
        current_path = parents + [title]
        paragraphs = [node_text(p) for p in sec.findall("./p")]
        paragraphs = [p for p in paragraphs if p]
        if paragraphs:
            yield " > ".join(current_path), "\n".join(paragraphs)
        yield from sections(sec, current_path)


def split_text(text: str, max_chars: int = 1800) -> list[str]:
    sentences = re.split(r"(?<=[。！？.!?])\s+", text)
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        if len(current) + len(sentence) + 1 > max_chars and current:
            chunks.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        chunks.append(current)
    return chunks


def load_metadata(path: str | None) -> dict[str, dict[str, object]]:
    """Index collector metadata by PMC identifier and DOI for provenance carry-over."""
    index: dict[str, dict[str, object]] = {}
    if not path:
        return index
    with Path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            record = json.loads(line)
            for key in (str(record.get("pmcid", "")), str(record.get("doi", ""))):
                if key:
                    index[key.lower()] = record
    return index


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="Directory containing JATS XML files")
    parser.add_argument("--metadata", help="Optional Europe PMC metadata JSONL from collector.py")
    parser.add_argument("--output", required=True, help="Output JSONL path")
    args = parser.parse_args()
    input_dir = Path(args.input)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    metadata_index = load_metadata(args.metadata)
    count = 0
    with output.open("w", encoding="utf-8") as handle:
        for xml_path in input_dir.glob("*.xml"):
            try:
                root = ET.parse(xml_path).getroot()
            except ET.ParseError:
                continue
            meta = article_metadata(root)
            collector_meta = metadata_index.get(meta["pmcid"].lower()) or metadata_index.get(meta["doi"].lower(), {})
            body = root.find(".//body")
            if body is None:
                continue
            for section_path, text in sections(body):
                for position, chunk in enumerate(split_text(text), start=1):
                    count += 1
                    record = {
                        "chunk_id": f"{xml_path.stem}_{count}",
                        "text": chunk,
                        "section": section_path,
                        "chunk_position": position,
                        "source_file": xml_path.name,
                        "title": meta["title"],
                        "doi": meta["doi"],
                        "pmcid": meta["pmcid"],
                        "source_url": (
                            f"https://pmc.ncbi.nlm.nih.gov/articles/{meta['pmcid'] if meta['pmcid'].upper().startswith('PMC') else 'PMC' + meta['pmcid']}/"
                            if meta["pmcid"] else str(collector_meta.get("source_url", ""))
                        ),
                        "license": collector_meta.get("license", ""),
                        "is_open_access": collector_meta.get("is_open_access", True),
                        "sequences": collector_meta.get("sequences", []),
                        "diseases": collector_meta.get("diseases", []),
                        "disease_categories": collector_meta.get("disease_categories", []),
                        "doc_types": collector_meta.get("doc_types", []),
                        "curated_disease_category": collector_meta.get("curated_disease_category", ""),
                        "curated_doc_type": collector_meta.get("curated_doc_type", ""),
                        "field_strengths_t": collector_meta.get("field_strengths_t", []),
                        "low_field_direct_evidence": collector_meta.get("low_field_direct_evidence", False),
                        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                    }
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(f"Wrote {count} chunks to {output}")


if __name__ == "__main__":
    main()
