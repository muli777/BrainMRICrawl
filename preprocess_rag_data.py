"""Preprocess Europe PMC XML full-text data for RAG knowledge base.

Extracts article metadata and text content from XML files, cleans the text,
and outputs structured JSON records for downstream RAG processing.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Optional
from xml.etree import ElementTree as ET


NSMAP = {
    "default": "http://www.ncbi.nlm.nih.gov/pmc/articleset",
    "xlink": "http://www.w3.org/1999/xlink",
}


def parse_xml_file(filepath: Path) -> Optional[dict[str, Any]]:
    try:
        tree = ET.parse(filepath)
        root = tree.getroot()
    except ET.ParseError as exc:
        print(f"[FAIL] XML parse error: {filepath.name} - {exc}")
        return None
    except Exception as exc:
        print(f"[FAIL] Unexpected error: {filepath.name} - {type(exc).__name__}: {exc}")
        return None

    ns = root.tag.split("}")[0].strip("{") if "}" in root.tag else ""

    def _find(elem, path):
        if ns:
            parts = path.split("/")
            ns_path = "/".join(f"{{{ns}}}{part}" for part in parts)
            return elem.find(ns_path)
        return elem.find(path)

    def _findall(elem, path):
        if ns:
            parts = path.split("/")
            ns_path = "/".join(f"{{{ns}}}{part}" for part in parts)
            return elem.findall(ns_path)
        return elem.findall(path)

    article = root
    if article.tag != "article" and not article.tag.endswith("}article"):
        article = root.find("article")
        if ns:
            article = article or root.find(f"{{{ns}}}article")
        if article is None:
            print(f"[FAIL] Non-article XML format: {filepath.name} - root tag is {root.tag}")
            return None

    result: dict[str, Any] = {}

    front = _find(article, "front")
    if front is not None:
        journal_meta = _find(front, "journal-meta")
        if journal_meta is not None:
            journal_title = _find(journal_meta, "journal-title-group/journal-title")
            result["journal"] = _text_or_empty(journal_title)

        article_meta = _find(front, "article-meta")
        if article_meta is not None:
            for article_id in _findall(article_meta, "article-id"):
                id_type = article_id.attrib.get("pub-id-type", "")
                id_value = _text_or_empty(article_id)
                if id_type == "pmcid":
                    result["pmcid"] = id_value
                elif id_type == "pmid":
                    result["pmid"] = id_value
                elif id_type == "doi":
                    result["doi"] = id_value
                elif id_type == "archive":
                    result["pprid"] = id_value

            title_group = _find(article_meta, "title-group")
            if title_group is not None:
                article_title = _find(title_group, "article-title")
                result["title"] = _text_or_empty(article_title)

            contrib_group = _find(front, "contrib-group")
            if contrib_group is None:
                contrib_group = _find(article_meta, "contrib-group")

            if contrib_group is not None:
                authors = []
                for contrib in _findall(contrib_group, "contrib"):
                    name = _find(contrib, "name")
                    if name is not None:
                        surname = _find(name, "surname")
                        given_names = _find(name, "given-names")
                        authors.append(f"{_text_or_empty(given_names)} {_text_or_empty(surname)}".strip())
                result["authors"] = authors

            pub_date = _find(article_meta, "pub-date")
            if pub_date is not None:
                year = _find(pub_date, "year")
                result["publication_year"] = _text_or_empty(year)

            abstract_elem = _find(article_meta, "abstract")
            if abstract_elem is not None:
                result["abstract"] = _clean_text(_extract_text(abstract_elem, ns))

            keywords = _find(article_meta, "kwd-group")
            if keywords is None:
                keywords = _find(article_meta, ".//kwd-group")
            if keywords is not None:
                result["keywords"] = [_text_or_empty(k) for k in _findall(keywords, "kwd") if _text_or_empty(k)]

    result["text_content"] = _extract_full_text(article, ns)
    result["source_file"] = filepath.name

    if not result.get("title"):
        result["title"] = "Untitled"

    return result


def _text_or_empty(elem: Optional[ET.Element]) -> str:
    return elem.text.strip() if elem is not None and elem.text else ""


def _extract_text(elem: ET.Element, ns: str = "") -> str:
    text_parts = []
    target_tags = ("p", "title", "label", "caption", "cell", "th", "td")
    for child in elem.iter():
        tag_name = child.tag.split("}")[-1] if "}" in child.tag else child.tag
        if tag_name in target_tags:
            text = " ".join(child.itertext()).strip()
            if text:
                text_parts.append(text)
    return "\n".join(text_parts)


def _extract_full_text(article: ET.Element, ns: str = "") -> str:
    sections = []

    def _find(elem, path):
        if ns:
            parts = path.split("/")
            ns_path = "/".join(f"{{{ns}}}{part}" for part in parts)
            return elem.find(ns_path)
        return elem.find(path)

    def _findall(elem, path):
        if ns:
            parts = path.split("/")
            ns_path = "/".join(f"{{{ns}}}{part}" for part in parts)
            return elem.findall(ns_path)
        return elem.findall(path)

    abstract_elem = _find(article, "front/article-meta/abstract")
    if abstract_elem is None:
        abstract_elem = _find(article, "front/abstract")
    if abstract_elem is not None:
        abstract_text = _extract_text(abstract_elem, ns)
        if abstract_text:
            sections.append(f"ABSTRACT\n{abstract_text}")

    body = _find(article, "body")
    if body is not None:
        for sec in _findall(body, "sec"):
            title = _find(sec, "title")
            title_text = _text_or_empty(title)
            content = _extract_text(sec, ns)
            if content:
                if title_text:
                    sections.append(f"{title_text}\n{content}")
                else:
                    sections.append(content)

    back = _find(article, "back")
    if back is not None:
        for sec in _findall(back, "sec"):
            title = _find(sec, "title")
            title_text = _text_or_empty(title)
            content = _extract_text(sec, ns)
            if content and title_text:
                sections.append(f"{title_text}\n{content}")

    return "\n\n".join(sections)


def _clean_text(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s([.,;:!?])", r"\1", text)
    text = re.sub(r"([.,;:!?])\s+", r"\1 ", text)
    text = text.strip()
    return text


def _filter_low_quality(article: dict[str, Any]) -> bool:
    return True


def _infer_topics(article: dict[str, Any]) -> list[str]:
    topics = []
    text = (article.get("title", "") + " " + article.get("abstract", "") + " " + article.get("text_content", "")).lower()

    cerebrovascular_patterns = [
        r"\bstroke\b", r"\bischemi[ac]\b", r"\binfarct\b", r"\bhemorrhage\b",
        r"\bsah\b", r"\bich\b", r"\bcerebrovascular\b", r"\bmoyamoya\b",
        r"\baneurysm\b", r"\bavm\b", r"\btransient ischemic\b"
    ]
    if any(re.search(p, text) for p in cerebrovascular_patterns):
        topics.append("cerebrovascular")

    tumor_patterns = [
        r"\bglioma\b", r"\bglioblastoma\b", r"\bmeningioma\b", r"\bbrain tumor\b",
        r"\bbrain neoplasm\b", r"\bmetastasis\b", r"\bcns tumor\b", r"\bastrocytoma\b",
        r"\bmedulloblastoma\b", r"\bependymoma\b"
    ]
    if any(re.search(p, text) for p in tumor_patterns):
        topics.append("brain_tumor")

    neurology_patterns = [
        r"\bmultiple sclerosis\b", r"\bms\b", r"\bepilepsy\b", r"\bseizure\b",
        r"\bparkinson\b", r"\balzheimer\b", r"\bdementia\b", r"\bencephalitis\b",
        r"\bneuromyelitis\b", r"\bals\b", r"\bfrontotemporal\b", r"\bmigraine\b"
    ]
    if any(re.search(p, text) for p in neurology_patterns):
        topics.append("neurology")

    psychiatry_patterns = [
        r"\bschizophrenia\b", r"\bdepression\b", r"\bbipolar\b", r"\bautism\b",
        r"\bpsychiatric\b", r"\bocd\b", r"\bpsychosis\b", r"\bpdd\b", r"\bptsd\b"
    ]
    if any(re.search(p, text) for p in psychiatry_patterns):
        topics.append("psychiatry")

    mri_patterns = [
        r"\bmri\b", r"\bmagnetic resonance\b", r"\bt1-weighted\b", r"\bt2-weighted\b",
        r"\bflair\b", r"\bdwi\b", r"\badc\b", r"\bswi\b", r"\bmra\b", r"\bmrs\b",
        r"\basl\b", r"\bpwi\b", r"\bdti\b", r"\bfmri\b"
    ]
    if any(re.search(p, text) for p in mri_patterns):
        topics.append("mri")

    return topics


def preprocess_directory(input_dir: Path, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    xml_files = sorted(input_dir.glob("*.xml"))
    print(f"Found {len(xml_files)} XML files in {input_dir}")

    output_file = output_dir / "preprocessed_articles.jsonl"
    stats_file = output_dir / "preprocessing_stats.json"

    stats = {
        "total_files": len(xml_files),
        "success": 0,
        "failed": 0,
        "filtered": 0,
        "total_characters": 0,
        "topics": {},
        "journals": {}
    }

    with output_file.open("w", encoding="utf-8") as out_handle:
        for i, xml_file in enumerate(xml_files, 1):
            print(f"Processing [{i}/{len(xml_files)}]: {xml_file.name}")

            article = parse_xml_file(xml_file)
            if article is None:
                stats["failed"] += 1
                continue

            if not _filter_low_quality(article):
                stats["filtered"] += 1
                continue

            article["text_content"] = _clean_text(article["text_content"])
            article["topics"] = _infer_topics(article)
            article["text_length"] = len(article["text_content"])

            stats["success"] += 1
            stats["total_characters"] += article["text_length"]

            for topic in article["topics"]:
                stats["topics"][topic] = stats["topics"].get(topic, 0) + 1

            journal = article.get("journal", "")
            if journal:
                stats["journals"][journal] = stats["journals"].get(journal, 0) + 1

            out_handle.write(json.dumps(article, ensure_ascii=False) + "\n")

    with stats_file.open("w", encoding="utf-8") as stats_handle:
        json.dump(stats, stats_handle, ensure_ascii=False, indent=2)

    print("\n=== Preprocessing Complete ===")
    print(f"Total files: {stats['total_files']}")
    print(f"Success: {stats['success']}")
    print(f"Failed: {stats['failed']}")
    print(f"Filtered: {stats['filtered']}")
    print(f"Total characters: {stats['total_characters']:,}")
    print(f"\nTopics distribution:")
    for topic, count in sorted(stats["topics"].items(), key=lambda x: -x[1]):
        print(f"  {topic}: {count}")
    print(f"\nTop journals:")
    for journal, count in sorted(stats["journals"].items(), key=lambda x: -x[1])[:10]:
        print(f"  {journal}: {count}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Preprocess Europe PMC XML data for RAG")
    parser.add_argument(
        "--input-dir",
        type=str,
        default="DataClean/europepmc_fulltext",
        help="Directory containing XML files"
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="DataClean/preprocessed",
        help="Directory to save preprocessed data"
    )
    args = parser.parse_args()

    input_path = Path(args.input_dir)
    output_path = Path(args.output_dir)

    if not input_path.exists():
        print(f"Error: Input directory '{input_path}' does not exist")
        return

    preprocess_directory(input_path, output_path)


if __name__ == "__main__":
    main()