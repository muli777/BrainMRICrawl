"""Clean JATS/XML research articles into readable, RAG-ready UTF-8 text files.

The script keeps article title, abstract, body section headings and paragraphs.
It excludes XML/web presentation elements, references, figures, tables, formulas,
acknowledgements, disclosures and other non-clinical article back matter.
"""

from __future__ import annotations

import argparse
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Iterable


REMOVE_TAGS = {
    "ack", "app", "attrib", "award-group", "bio", "copyright-statement",
    "custom-meta-group", "disp-formula", "disp-formula-group", "fig", "fn",
    "fn-group", "funding-group", "graphic", "media", "permissions", "ref",
    "ref-list", "related-article", "response", "supplementary-material",
    "table-wrap", "table-wrap-group", "tex-math", "math", "mml:math",
}
REMOVE_SECTION_TITLES = re.compile(
    r"^(references?|bibliography|acknowledg(e)?ments?|funding|funding information|"
    r"conflict(s)? of interest|competing interests?|disclosure(s)?|author contributions?|"
    r"data availability|ethical approval|ethics statement|supplementary (material|information)|"
    r"notes?)$",
    flags=re.IGNORECASE,
)
INLINE_NOISE_TAGS = {"xref", "ext-link", "uri", "sup", "sub", "label"}


def local_name(tag: str) -> str:
    """Return an XML tag without its namespace prefix."""
    return tag.rsplit("}", 1)[-1].split(":")[-1]


def normalize(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    # Numeric/bracketed citations are not useful as standalone RAG evidence.
    text = re.sub(r"\s*(?:\[[0-9,;\-– ]+\]|\([0-9,;\-– ]+\))", "", text)
    return re.sub(r"\s+", " ", text).strip()


def remove_node(parent: ET.Element, child: ET.Element) -> None:
    """Remove child while retaining its tail text, if any."""
    children = list(parent)
    index = children.index(child)
    if child.tail:
        if index:
            previous = children[index - 1]
            previous.tail = (previous.tail or "") + child.tail
        else:
            parent.text = (parent.text or "") + child.tail
    parent.remove(child)


def prune_noise(node: ET.Element) -> None:
    """Remove presentation-only and non-content nodes in place."""
    for child in list(node):
        tag = local_name(child.tag)
        if tag in REMOVE_TAGS:
            remove_node(node, child)
        elif tag in INLINE_NOISE_TAGS:
            # Keep surrounding sentence text but remove links and citation labels.
            remove_node(node, child)
        else:
            prune_noise(child)


def text_of(node: ET.Element | None) -> str:
    return "" if node is None else normalize(" ".join(node.itertext()))


def first_descendant(node: ET.Element, name: str) -> ET.Element | None:
    return next((item for item in node.iter() if local_name(item.tag) == name), None)


def direct_children(node: ET.Element, name: str) -> list[ET.Element]:
    return [item for item in node if local_name(item.tag) == name]


def section_blocks(container: ET.Element, level: int = 1) -> Iterable[str]:
    """Yield direct section paragraphs with headings, without nested duplication."""
    for sec in direct_children(container, "sec"):
        title_node = next(iter(direct_children(sec, "title")), None)
        title = text_of(title_node)
        if title and REMOVE_SECTION_TITLES.match(title):
            continue
        if title:
            yield f"{'#' * min(level, 6)} {title}"
        for paragraph in direct_children(sec, "p"):
            paragraph_text = text_of(paragraph)
            if paragraph_text:
                yield paragraph_text
        yield from section_blocks(sec, level + 1)


def clean_article(xml_path: Path, include_abstract: bool, min_chars: int) -> str:
    root = ET.parse(xml_path).getroot()
    prune_noise(root)
    blocks: list[str] = []
    article_title = first_descendant(root, "article-title")
    title = text_of(article_title)
    if title:
        blocks.append(f"# {title}")
    if include_abstract:
        abstract = first_descendant(root, "abstract")
        abstract_text = text_of(abstract)
        if abstract_text:
            blocks.extend(["## Abstract", abstract_text])
    body = first_descendant(root, "body")
    if body is not None:
        # Some JATS records have introductory paragraphs directly under <body>.
        for paragraph in direct_children(body, "p"):
            paragraph_text = text_of(paragraph)
            if paragraph_text:
                blocks.append(paragraph_text)
        blocks.extend(section_blocks(body))
    filtered = [block for block in blocks if block.startswith("#") or len(block) >= min_chars]
    return "\n\n".join(filtered).strip() + ("\n" if filtered else "")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="XML file or directory containing XML files")
    parser.add_argument("--output", required=True, help="Directory for cleaned .txt files")
    parser.add_argument("--min-chars", type=int, default=40, help="Discard non-heading text shorter than this")
    parser.add_argument("--no-abstract", action="store_true", help="Do not include the article abstract")
    parser.add_argument("--overwrite", action="store_true", help="Replace existing text files")
    args = parser.parse_args()

    source = Path(args.input)
    targets = [source] if source.is_file() else sorted(source.rglob("*.xml"))
    if not targets:
        raise SystemExit(f"No XML files found in: {source}")
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    written = skipped = failed = 0
    for xml_path in targets:
        destination = output_dir / f"{xml_path.stem}.txt"
        if destination.exists() and not args.overwrite:
            skipped += 1
            continue
        try:
            cleaned = clean_article(xml_path, include_abstract=not args.no_abstract, min_chars=args.min_chars)
            if not cleaned.strip():
                print(f"WARNING: no usable body text in {xml_path.name}")
                failed += 1
                continue
            destination.write_text(cleaned, encoding="utf-8")
            written += 1
        except (ET.ParseError, OSError) as exc:
            print(f"WARNING: failed to clean {xml_path.name}: {exc}")
            failed += 1
    print(f"Done. written={written}, skipped={skipped}, failed={failed}, output={output_dir}")


if __name__ == "__main__":
    main()
