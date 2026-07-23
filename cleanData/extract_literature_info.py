from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Dict, List, Optional

import xml.etree.ElementTree as ET


def extract_from_xml(xml_path: Path) -> Optional[Dict[str, str]]:
    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()
    except Exception as e:
        print(f"Failed to parse {xml_path}: {e}")
        return None
    
    def find_text(path: str) -> str:
        parts = path.split("/")
        current = root
        for part in parts:
            found = False
            for child in current:
                child_tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                if child_tag == part:
                    current = child
                    found = True
                    break
            if not found:
                return ""
        return "".join(current.itertext()).strip()
    
    journal_title = find_text("front/journal-meta/journal-title-group/journal-title")
    if not journal_title:
        for meta in root.findall(".//journal-meta"):
            for title in meta.findall(".//journal-title"):
                journal_title = "".join(title.itertext()).strip()
                if journal_title:
                    break
            if journal_title:
                break
    
    article_title = find_text("front/article-meta/title-group/article-title")
    if not article_title:
        for meta in root.findall(".//article-meta"):
            for title in meta.findall(".//article-title"):
                article_title = "".join(title.itertext()).strip()
                if article_title:
                    break
            if article_title:
                break
    
    keywords = ""
    for abstract in root.findall(".//abstract"):
        for sec in abstract.findall(".//sec"):
            for p in sec.findall(".//p"):
                text = "".join(p.itertext()).strip()
                if "Keywords" in text or "keywords" in text:
                    keywords = text.replace("Keywords:", "").replace("keywords:", "").replace("Keyword:", "").replace("keyword:", "").strip()
                    break
            if keywords:
                break
        if keywords:
            break
    
    if not keywords:
        for kwd in root.findall(".//kwd-group"):
            kwd_text = "".join(kwd.itertext()).strip()
            if kwd_text:
                keywords = kwd_text.replace("Keywords:", "").replace("keywords:", "").strip()
                break
    
    if journal_title or article_title:
        return {
            "pmcid": xml_path.stem.replace("PMC_", ""),
            "journal": journal_title,
            "title": article_title,
            "keywords": keywords,
        }
    
    return None


def extract_from_txt(txt_path: Path) -> Optional[Dict[str, str]]:
    try:
        content = txt_path.read_text(encoding="utf-8")
    except Exception as e:
        print(f"Failed to read {txt_path}: {e}")
        return None
    
    lines = content.split("\n")
    
    title = ""
    for line in lines:
        if line.startswith("# "):
            title = line[2:].strip()
            break
    
    keywords = ""
    for line in lines:
        if "Keywords:" in line or "keywords:" in line or "Keyword:" in line or "keyword:" in line:
            keywords = line.replace("Keywords:", "").replace("keywords:", "").replace("Keyword:", "").replace("keyword:", "").strip()
            break
    
    if title:
        return {
            "pmcid": txt_path.stem.replace("PMC_", ""),
            "journal": "",
            "title": title,
            "keywords": keywords,
        }
    
    return None


def extract_all_info(xml_dir: Path, txt_dir: Path) -> List[Dict[str, str]]:
    all_info = []
    xml_files = sorted(xml_dir.glob("*.xml"))
    
    for xml_file in xml_files:
        info = extract_from_xml(xml_file)
        if info:
            txt_file = txt_dir / f"{xml_file.stem}.txt"
            if txt_file.exists():
                txt_info = extract_from_txt(txt_file)
                if txt_info and txt_info.get("keywords"):
                    info["keywords"] = txt_info["keywords"]
            all_info.append(info)
            print(f"Extracted: {info['title'][:50]}...")
    
    return all_info


def generate_summary_doc(all_info: List[Dict[str, str]], output_file: Path, journal_stats_file: Path) -> None:
    content = "# 文献信息汇总\n\n"
    content += f"**文献总数**: {len(all_info)}\n\n"
    content += "---\n\n"
    
    journal_stats: Dict[str, List[Dict]] = {}
    for info in all_info:
        journal = info["journal"] or "未知期刊"
        if journal not in journal_stats:
            journal_stats[journal] = []
        journal_stats[journal].append(info)
    
    sorted_journals = sorted(journal_stats.items(), key=lambda x: -len(x[1]))
    
    for idx, (journal, papers) in enumerate(sorted_journals, 1):
        content += f"## {idx}. {journal}\n"
        content += f"**文献数量**: {len(papers)}\n\n"
        
        for p_idx, paper in enumerate(papers, 1):
            content += f"### {idx}.{p_idx} {paper['title']}\n"
            content += f"- **PMCID**: {paper['pmcid']}\n"
            if paper["keywords"]:
                content += f"- **关键词**: {paper['keywords']}\n"
            else:
                content += f"- **关键词**: 未提取到\n"
            content += "\n"
        
        content += "---\n\n"
    
    output_file.write_text(content, encoding="utf-8")
    print(f"\n汇总文档已生成: {output_file}")
    
    max_count_width = max(len(str(len(papers))) for _, papers in sorted_journals)
    
    journal_content = ""
    for idx, (journal, papers) in enumerate(sorted_journals, 1):
        count_str = str(len(papers)).rjust(max_count_width)
        journal_content += f"{journal} | {count_str}\n"
    
    journal_stats_file.write_text(journal_content, encoding="utf-8")
    print(f"期刊统计文件已生成: {journal_stats_file}")


def main() -> None:
    parser = argparse.ArgumentParser(description="提取文献信息并生成汇总文档")
    parser.add_argument("--xml-dir", default="europepmc_fulltext", help="XML文件目录")
    parser.add_argument("--txt-dir", default="cleantext", help="TXT文件目录")
    parser.add_argument("--output", default="literature_summary.md", help="输出汇总文档路径")
    args = parser.parse_args()
    
    xml_dir = Path(args.xml_dir)
    txt_dir = Path(args.txt_dir)
    
    if not xml_dir.exists():
        raise SystemExit(f"XML目录不存在: {xml_dir}")
    if not txt_dir.exists():
        raise SystemExit(f"TXT目录不存在: {txt_dir}")
    
    print("开始提取文献信息...")
    all_info = extract_all_info(xml_dir, txt_dir)
    print(f"共提取 {len(all_info)} 篇文献信息")
    
    print("\n生成汇总文档...")
    output_file = Path(args.output)
    journal_stats_file = output_file.parent / "journal_counts.txt"
    generate_summary_doc(all_info, output_file, journal_stats_file)
    
    print("\n完成！")


if __name__ == "__main__":
    main()
