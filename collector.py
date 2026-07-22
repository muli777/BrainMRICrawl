"""Collect PubMed metadata and Europe PMC open-access full text for brain MRI RAG.

Only official public APIs are used.  The script deliberately has no browser,
cookie, login, proxy, CAPTCHA, or paywall-bypass code.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import time
import urllib.parse
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Iterable


NCBI_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
EUROPEPMC_BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest"
USER_AGENT = "brain-mri-rag-collector/0.1 (contact configured in collection_config.json)"

SEQUENCES = ("T1", "T2", "FLAIR", "DWI", "ADC", "SWI", "MRA", "MRS", "ASL", "PWI", "DTI", "fMRI")
DISEASE_PATTERNS = {
    "ischemic stroke": r"\b(ischemi[ac]\s+stroke|cerebral\s+infarct|acute\s+ischemi)\w*\b",
    "hemorrhagic stroke": r"\b(intracerebral\s+hemorrhage|ICH|subarachnoid\s+hemorrhage|SAH|hemorrhagic\s+stroke)\b",
    "cerebrovascular other": r"\b(stroke|cerebrovascular|moyamoya|cerebral\s+aneurysm|AVM|arteriovenous\s+malformation)\b",
    "glioma": r"\b(glioma|glioblastoma|astrocytoma|oligodendroglioma|GBM)\b",
    "meningioma": r"\b(meningioma)\b",
    "brain metastasis": r"\b(brain\s+metastas\w*|intracranial\s+metastas\w*)\b",
    "brain tumor other": r"\b(brain\s+tumou?r|CNS\s+tumou?r|brain\s+neoplasm|primary\s+brain\s+tumou?r)\b",
    "multiple sclerosis": r"\b(multiple\s+sclerosis|\bMS\b|demyelinat\w*|neuromyelitis)\b",
    "epilepsy": r"\b(epilep\w*|seizure|hippocampal\s+sclerosis)\b",
    "neurodegenerative": r"\b(Parkinson|Alzheimer|dementia|frontotemporal|ALS|amyotrophic\s+lateral\s+sclerosis)\b",
    "encephalitis": r"\b(encephalitis|autoimmune\s+encephalitis)\b",
    "schizophrenia": r"\b(schizophreni\w*)\b",
    "depression": r"\b(major\s+depressive|depression|MDD)\b",
    "bipolar disorder": r"\b(bipolar\s+disorder|bipolar)\b",
    "autism": r"\b(autism|ASD|autistic)\b",
    "psychiatric other": r"\b(psychiatric|OCD|obsessive[- ]compulsive|psychosis)\b",
}
CATEGORY_PATTERNS = {
    "cerebrovascular": r"\b(stroke|ischemi[ac]|infarct|hemorrhage|SAH|ICH|cerebrovascular|moyamoya|aneurysm|AVM)\b",
    "brain_tumor": r"\b(glioma|glioblastoma|meningioma|brain\s+tumou?r|brain\s+neoplasm|metastas\w*|CNS\s+tumou?r|astrocytoma)\b",
    "neurology": r"\b(multiple\s+sclerosis|epilep\w*|Parkinson|Alzheimer|dementia|encephalitis|neuromyelitis|ALS|frontotemporal)\b",
    "psychiatry": r"\b(schizophreni\w*|depress\w*|bipolar|autism|psychiatric|OCD|psychosis)\b",
}
DOC_TYPE_PATTERNS = {
    "guideline": r"\b(guideline|practice\s+guideline|consensus|recommendation|WHO\s+classification)\b",
    "review": r"\b(systematic\s+review|meta[- ]analysis|narrative\s+review|\breview\b)\b",
    "book": r"\b(textbook|handbook|books?\s+and\s+documents|\bbook\b)\b",
    "journal_article": r"\b(journal\s+article|original\s+article|clinical\s+trial|observational)\b",
}


def format_elapsed(seconds: float) -> str:
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    if hours > 0:
        return f"{hours}小时{minutes}分钟{secs:.2f}秒"
    elif minutes > 0:
        return f"{minutes}分钟{secs:.2f}秒"
    else:
        return f"{secs:.2f}秒"


class CrawlProgress:
    def __init__(self, log_file: Path) -> None:
        self.log_file = log_file
        self.total_count = 0
        self.success_count = 0
        self.failure_count = 0
        self.skipped_count = 0
        self.batch_success = 0
        self.batch_failure = 0
        self.batch_skipped = 0
        self.last_batch_start = 0
        self.overall_start = 0
        self.start_time = dt.datetime.now()

    def start(self) -> None:
        self.overall_start = 0
        self.last_batch_start = 0
        self.start_time = dt.datetime.now()
        message = f"开始爬取任务 - {self.start_time.strftime('%Y-%m-%d %H:%M:%S')}"
        print(f"\n{'='*60}")
        print(message)
        print(f"{'='*60}")
        with self.log_file.open("a", encoding="utf-8") as f:
            f.write(f"{message}\n")

    def increment(self, status: str) -> None:
        self.total_count += 1
        if status == "success":
            self.success_count += 1
            self.batch_success += 1
        elif status == "failure":
            self.failure_count += 1
            self.batch_failure += 1
        elif status == "skipped":
            self.skipped_count += 1
            self.batch_skipped += 1

        current_batch = (self.total_count - 1) // 100
        if self.total_count % 100 == 0:
            batch_start = current_batch * 100
            batch_end = self.total_count
            self._print_batch_report(batch_start, batch_end)
            self._write_batch_report(batch_start, batch_end)
            self.batch_success = 0
            self.batch_failure = 0
            self.batch_skipped = 0
            self.last_batch_start = self.total_count

        if self.total_count % 1000 == 0:
            self._print_summary_report(0, self.total_count)
            self._write_summary_report(0, self.total_count)

    def _print_batch_report(self, start: int, end: int) -> None:
        elapsed = (dt.datetime.now() - self.start_time).total_seconds()
        print(f"\n{'='*60}")
        print(f"批次报告 [{start}-{end}]")
        print(f"{'='*60}")
        print(f"  成功: {self.batch_success}")
        print(f"  失败: {self.batch_failure}")
        print(f"  跳过: {self.batch_skipped}")
        print(f"  累计: 成功={self.success_count}, 失败={self.failure_count}, 跳过={self.skipped_count}")
        print(f"  耗时: {format_elapsed(elapsed)}")
        print(f"{'='*60}")

    def _print_summary_report(self, start: int, end: int) -> None:
        elapsed = (dt.datetime.now() - self.start_time).total_seconds()
        print(f"\n{'#'*60}")
        print(f"汇总报告 [{start}-{end}]")
        print(f"{'#'*60}")
        print(f"  总计处理: {end - start}")
        print(f"  成功: {self.success_count}")
        print(f"  失败: {self.failure_count}")
        print(f"  跳过: {self.skipped_count}")
        print(f"  成功率: {(self.success_count / self.total_count * 100):.2f}%")
        print(f"  总耗时: {format_elapsed(elapsed)}")
        print(f"{'#'*60}")

    def _write_batch_report(self, start: int, end: int) -> None:
        elapsed = (dt.datetime.now() - self.start_time).total_seconds()
        with self.log_file.open("a", encoding="utf-8") as f:
            f.write(f"\n{'='*60}\n")
            f.write(f"批次报告 [{start}-{end}]\n")
            f.write(f"时间: {dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"{'='*60}\n")
            f.write(f"  成功: {self.batch_success}\n")
            f.write(f"  失败: {self.batch_failure}\n")
            f.write(f"  跳过: {self.batch_skipped}\n")
            f.write(f"  累计: 成功={self.success_count}, 失败={self.failure_count}, 跳过={self.skipped_count}\n")
            f.write(f"  耗时: {format_elapsed(elapsed)}\n")
            f.write(f"{'='*60}\n")

    def _write_summary_report(self, start: int, end: int) -> None:
        elapsed = (dt.datetime.now() - self.start_time).total_seconds()
        with self.log_file.open("a", encoding="utf-8") as f:
            f.write(f"\n{'#'*60}\n")
            f.write(f"汇总报告 [{start}-{end}]\n")
            f.write(f"时间: {dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"{'#'*60}\n")
            f.write(f"  总计处理: {end - start}\n")
            f.write(f"  成功: {self.success_count}\n")
            f.write(f"  失败: {self.failure_count}\n")
            f.write(f"  跳过: {self.skipped_count}\n")
            f.write(f"  成功率: {(self.success_count / self.total_count * 100):.2f}%\n")
            f.write(f"  总耗时: {format_elapsed(elapsed)}\n")
            f.write(f"{'#'*60}\n")

    def final_report(self) -> None:
        elapsed = (dt.datetime.now() - self.start_time).total_seconds()
        print(f"\n{'#'*60}")
        print(f"最终汇总报告 [0-{self.total_count}]")
        print(f"{'#'*60}")
        print(f"  总计处理: {self.total_count}")
        print(f"  成功: {self.success_count}")
        print(f"  失败: {self.failure_count}")
        print(f"  跳过: {self.skipped_count}")
        print(f"  成功率: {(self.success_count / self.total_count * 100):.2f}%")
        print(f"  总耗时: {format_elapsed(elapsed)}")
        print(f"  结束时间: {dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{'#'*60}")

        with self.log_file.open("a", encoding="utf-8") as f:
            f.write(f"\n{'#'*60}\n")
            f.write(f"最终汇总报告 [0-{self.total_count}]\n")
            f.write(f"时间: {dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"{'#'*60}\n")
            f.write(f"  总计处理: {self.total_count}\n")
            f.write(f"  成功: {self.success_count}\n")
            f.write(f"  失败: {self.failure_count}\n")
            f.write(f"  跳过: {self.skipped_count}\n")
            f.write(f"  成功率: {(self.success_count / self.total_count * 100):.2f}%\n")
            f.write(f"  总耗时: {format_elapsed(elapsed)}\n")
            f.write(f"{'#'*60}\n")


def normalize_query_entry(entry: str | dict[str, Any]) -> dict[str, str]:
    if isinstance(entry, str):
        return {"query": entry, "label": "", "disease_category": "", "doc_type": ""}
    query = str(entry.get("query", "")).strip()
    if not query:
        raise ValueError(f"Query entry missing 'query' field: {entry!r}")
    return {
        "query": query,
        "label": str(entry.get("label", "")),
        "disease_category": str(entry.get("disease_category", "")),
        "doc_type": str(entry.get("doc_type", "")),
    }


class HttpClient:
    def __init__(self, email: str, min_interval_seconds: float = 0.4) -> None:
        self.email = email
        self.min_interval_seconds = min_interval_seconds
        self._last_request = 0.0

    def get(self, url: str, params: dict[str, Any] | None = None) -> bytes:
        if params:
            url = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"
        wait = self.min_interval_seconds - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/xml, application/json, text/plain"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = response.read()
        finally:
            self._last_request = time.monotonic()
        return payload


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def text_of(element: ET.Element | None) -> str:
    return "" if element is None else " ".join(" ".join(element.itertext()).split())


def infer_doc_types(text: str, publication_types: list[str] | None = None) -> list[str]:
    blob = " ".join([text, " ".join(publication_types or [])])
    found = [name for name, pattern in DOC_TYPE_PATTERNS.items() if re.search(pattern, blob, flags=re.I)]
    pubs = " ".join(publication_types or []).lower()
    if "guideline" in pubs or "consensus" in pubs:
        found.append("guideline")
    if "review" in pubs or "meta-analysis" in pubs:
        found.append("review")
    if "book" in pubs:
        found.append("book")
    return list(dict.fromkeys(found))


def extract_tags(text: str, publication_types: list[str] | None = None) -> dict[str, list[str] | bool]:
    upper = text.upper()
    sequences = [sequence for sequence in SEQUENCES if re.search(rf"\b{re.escape(sequence)}\b", upper)]
    diseases = [name for name, pattern in DISEASE_PATTERNS.items() if re.search(pattern, text, flags=re.I)]
    disease_categories = [name for name, pattern in CATEGORY_PATTERNS.items() if re.search(pattern, text, flags=re.I)]
    doc_types = infer_doc_types(text, publication_types)
    strengths = re.findall(r"\b(?:0\.0?64|0\.1|0\.2|0\.3|0\.5|0\.55|1\.5|3(?:\.0)?|7(?:\.0)?)\s*T\b", text, flags=re.I)
    low_field = bool(re.search(r"\b(low[- ]field|ultra[- ]low[- ]field|portable MRI)\b", text, flags=re.I))
    return {
        "sequences": sequences,
        "diseases": diseases,
        "disease_categories": disease_categories,
        "doc_types": doc_types,
        "field_strengths_t": strengths,
        "low_field_direct_evidence": low_field,
    }


def load_existing_ids(output_dir: Path) -> set[str]:
    existing_ids = set()
    pubmed_metadata = output_dir / "pubmed_metadata.jsonl"
    europepmc_metadata = output_dir / "europepmc_metadata.jsonl"

    for metadata_file in [pubmed_metadata, europepmc_metadata]:
        if metadata_file.exists():
            with metadata_file.open("r", encoding="utf-8") as f:
                for line in f:
                    try:
                        record = json.loads(line)
                        if "pmid" in record and record["pmid"]:
                            existing_ids.add(f"pmid_{record['pmid']}")
                        if "pmcid" in record and record["pmcid"]:
                            existing_ids.add(f"pmcid_{record['pmcid']}")
                        if "europepmc_id" in record and record["europepmc_id"]:
                            existing_ids.add(f"epmc_{record['europepmc_id']}")
                    except json.JSONDecodeError:
                        continue

    return existing_ids


def pubmed_ids(client: HttpClient, query: str, limit: int, api_key: str) -> list[str]:


    params: dict[str, Any] = {"db": "pubmed", "term": query, "retmax": limit, "retmode": "json",
                              "tool": "brain_mri_rag_collector", "email": client.email}
    if api_key:
        params["api_key"] = api_key
    result = json.loads(client.get(f"{NCBI_BASE}/esearch.fcgi", params))
    return result.get("esearchresult", {}).get("idlist", [])


def pubmed_records(
    client: HttpClient,
    ids: list[str],
    query_meta: dict[str, str],
    api_key: str,
) -> Iterable[dict[str, Any]]:
    query = query_meta["query"]
    for start in range(0, len(ids), 100):
        params: dict[str, Any] = {"db": "pubmed", "id": ",".join(ids[start:start + 100]), "retmode": "xml", "tool": "brain_mri_rag_collector", "email": client.email}
        if api_key:
            params["api_key"] = api_key
        root = ET.fromstring(client.get(f"{NCBI_BASE}/efetch.fcgi", params))
        for article in root.findall(".//PubmedArticle"):
            medline = article.find("MedlineCitation")
            article_data = medline.find("Article") if medline is not None else None
            pmid = text_of(medline.find("PMID") if medline is not None else None)
            title = text_of(article_data.find("ArticleTitle") if article_data is not None else None)
            abstract = " ".join(text_of(node) for node in (article_data.findall("Abstract/AbstractText") if article_data is not None else []))
            ids_by_type = {node.attrib.get("IdType", ""): text_of(node) for node in article.findall("PubmedData/ArticleIdList/ArticleId")}
            mesh = [text_of(node) for node in (medline.findall("MeshHeadingList/MeshHeading/DescriptorName") if medline is not None else [])]
            publication_types = [text_of(node) for node in (article_data.findall("PublicationTypeList/PublicationType") if article_data is not None else [])]
            journal = text_of(article_data.find("Journal/Title") if article_data is not None else None)
            year = text_of(article_data.find("Journal/JournalIssue/PubDate/Year") if article_data is not None else None)
            combined = " ".join([title, abstract, " ".join(mesh)])
            tags = extract_tags(combined, publication_types)
            if query_meta.get("disease_category") and query_meta["disease_category"] != "general":
                tags["disease_categories"] = list(dict.fromkeys([query_meta["disease_category"], *tags["disease_categories"]]))
            if query_meta.get("doc_type"):
                tags["doc_types"] = list(dict.fromkeys([query_meta["doc_type"], *tags["doc_types"]]))
            yield {
                "source": "PubMed",
                "query": query,
                "query_label": query_meta.get("label", ""),
                "curated_disease_category": query_meta.get("disease_category", ""),
                "curated_doc_type": query_meta.get("doc_type", ""),
                "retrieved_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "pmid": pmid,
                "pmcid": ids_by_type.get("pmc", ""),
                "doi": ids_by_type.get("doi", ""),
                "title": title,
                "abstract": abstract,
                "mesh_terms": mesh,
                "publication_types": publication_types,
                "journal": journal,
                "year": year,
                "source_url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                **tags,
            }


def europepmc_search(client: HttpClient, query: str, limit: int) -> list[dict[str, Any]]:

    payload = client.get(f"{EUROPEPMC_BASE}/search",
                         {"query": query, "format": "json", "resultType": "core", "pageSize": min(limit, 1000)})
    return json.loads(payload).get("resultList", {}).get("result", [])[:limit]


def normalize_europepmc(record: dict[str, Any], query_meta: dict[str, str]) -> dict[str, Any]:
    query = query_meta["query"]
    combined = " ".join(str(record.get(key, "")) for key in ("title", "abstractText", "keywordList", "meshHeadingList"))
    source = record.get("source", "")
    identifier = record.get("id", "")
    pmcid = record.get("pmcid", "")
    fulltext_source = "PMC" if pmcid else source
    fulltext_id = pmcid or identifier
    pub_types = record.get("pubTypeList", {})
    if isinstance(pub_types, dict):
        publication_types = [str(x) for x in pub_types.get("pubType", [])]
    elif isinstance(pub_types, list):
        publication_types = [str(x) for x in pub_types]
    else:
        publication_types = []
    tags = extract_tags(combined, publication_types)
    if query_meta.get("disease_category") and query_meta["disease_category"] != "general":
        tags["disease_categories"] = list(dict.fromkeys([query_meta["disease_category"], *tags["disease_categories"]]))
    if query_meta.get("doc_type"):
        tags["doc_types"] = list(dict.fromkeys([query_meta["doc_type"], *tags["doc_types"]]))
    return {
        "source": "Europe PMC",
        "query": query,
        "query_label": query_meta.get("label", ""),
        "curated_disease_category": query_meta.get("disease_category", ""),
        "curated_doc_type": query_meta.get("doc_type", ""),
        "retrieved_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "europepmc_source": source,
        "europepmc_id": identifier,
        "pmid": record.get("pmid", ""),
        "pmcid": pmcid,
        "doi": record.get("doi", ""),
        "title": record.get("title", ""),
        "abstract": record.get("abstractText", ""),
        "journal": record.get("journalTitle", ""),
        "year": record.get("pubYear", ""),
        "author_string": record.get("authorString", ""),
        "publication_types": publication_types,
        "is_open_access": str(record.get("isOpenAccess", "")).upper() == "Y",
        "license": record.get("license", ""),
        "source_url": f"https://europepmc.org/article/{source}/{identifier}",
        "fulltext_source": fulltext_source,
        "fulltext_id": fulltext_id,
        **tags,
    }


def safe_filename(source: str, identifier: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", f"{source}_{identifier}") + ".xml"


def download_europepmc_xml(client: HttpClient, record: dict[str, Any], directory: Path) -> tuple[bool, str]:
    if not record["is_open_access"]:
        return False, "not_open_access"
    source, identifier = record["fulltext_source"], record["fulltext_id"]
    if not source or not identifier:
        return False, "missing_source_or_id"
    destination = directory / safe_filename(source, identifier)
    if destination.exists():
        return True, "already_exists"
    try:
        xml = client.get(f"{EUROPEPMC_BASE}/{identifier}/fullTextXML")
        if not xml.lstrip().startswith(b"<"):
            return False, "non_xml_response"
        destination.write_bytes(xml)
        return True, str(destination)
    except urllib.error.HTTPError as exc:
        return False, f"download_http_error:{exc.code}"
    except urllib.error.URLError as exc:
        return False, f"download_url_error:{exc.reason}"


def crawl_once(config: dict[str, Any]) -> CrawlProgress:
    email = config["email"]
    output = Path(config.get("output_dir", "data"))
    fulltext_dir = output / "europepmc_fulltext"
    output.mkdir(parents=True, exist_ok=True)
    fulltext_dir.mkdir(parents=True, exist_ok=True)
    client = HttpClient(email=email)
    limit = int(config.get("max_records_per_query", 100))
    api_key = config.get("ncbi_api_key", "")
    manifest = output / "collection_manifest.jsonl"
    log_file = output / "crawl_progress.txt"

    progress = CrawlProgress(log_file)
    progress.start()

    existing_ids = load_existing_ids(output)
    print(f"已加载 {len(existing_ids)} 条已爬取记录")

    for entry in config.get("pubmed_queries", []):
        try:
            query_meta = normalize_query_entry(entry)
            ids = pubmed_ids(client, query_meta["query"], limit, api_key)
            print(f"\n查询 [{query_meta['label']}]: 找到 {len(ids)} 条记录")

            for record in pubmed_records(client, ids, query_meta, api_key):
                try:
                    pmid = record.get("pmid", "")
                    pmcid = record.get("pmcid", "")

                    is_duplicate = False
                    if pmid and f"pmid_{pmid}" in existing_ids:
                        is_duplicate = True
                    elif pmcid and f"pmcid_{pmcid}" in existing_ids:
                        is_duplicate = True

                    if is_duplicate:
                        progress.increment("skipped")
                        print(f"跳过已爬取记录: PMID={pmid}")
                        continue

                    append_jsonl(output / "pubmed_metadata.jsonl", record)
                    if pmid:
                        existing_ids.add(f"pmid_{pmid}")
                    if pmcid:
                        existing_ids.add(f"pmcid_{pmcid}")
                    progress.increment("success")
                    print(f"成功爬取: {record.get('title', '')[:50]}...")
                except Exception as e:
                    progress.increment("failure")
                    print(f"爬取失败: PMID={record.get('pmid', '')}, 错误={str(e)[:100]}")

            append_jsonl(
                manifest,
                {
                    "source": "PubMed",
                    "query": query_meta["query"],
                    "query_label": query_meta["label"],
                    "disease_category": query_meta["disease_category"],
                    "doc_type": query_meta["doc_type"],
                    "records": len(ids),
                    "retrieved_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                },
            )
        except Exception as e:
            print(f"\n查询 [{entry.get('label', 'unknown')}] 执行出错，跳过: {str(e)[:100]}")

    for entry in config.get("europepmc_queries", []):
        try:
            query_meta = normalize_query_entry(entry)
            records = europepmc_search(client, query_meta["query"], limit)
            print(f"\n查询 [{query_meta['label']}]: 找到 {len(records)} 条记录")

            for source_record in records:
                try:
                    record = normalize_europepmc(source_record, query_meta)
                    pmid = record.get("pmid", "")
                    pmcid = record.get("pmcid", "")
                    epmc_id = record.get("europepmc_id", "")

                    is_duplicate = False
                    if pmid and f"pmid_{pmid}" in existing_ids:
                        is_duplicate = True
                    elif pmcid and f"pmcid_{pmcid}" in existing_ids:
                        is_duplicate = True
                    elif epmc_id and f"epmc_{epmc_id}" in existing_ids:
                        is_duplicate = True

                    if is_duplicate:
                        progress.increment("skipped")
                        print(f"跳过已爬取记录: EPMC={epmc_id}")
                        continue

                    status = "metadata_only"
                    append_jsonl(output / "europepmc_metadata.jsonl", record)

                    if config.get("download_open_access_fulltext", True):
                        ok, status = download_europepmc_xml(client, record, fulltext_dir)
                        record["fulltext_downloaded"] = ok

                    if pmid:
                        existing_ids.add(f"pmid_{pmid}")
                    if pmcid:
                        existing_ids.add(f"pmcid_{pmcid}")
                    if epmc_id:
                        existing_ids.add(f"epmc_{epmc_id}")

                    if ok or not config.get("download_open_access_fulltext", True):
                        progress.increment("success")
                        print(f"成功爬取: {record.get('title', '')[:50]}...")
                    else:
                        progress.increment("failure")
                        print(f"爬取失败: EPMC={epmc_id}, 状态={status}")
                except Exception as e:
                    progress.increment("failure")
                    print(f"爬取失败: EPMC={source_record.get('id', '')}, 错误={str(e)[:100]}")

                try:
                    append_jsonl(
                        manifest,
                        {
                            "source": "Europe PMC",
                            "id": record.get("europepmc_id", ""),
                            "query": query_meta["query"],
                            "query_label": query_meta["label"],
                            "disease_category": query_meta["disease_category"],
                            "doc_type": query_meta["doc_type"],
                            "status": status,
                            "retrieved_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                        },
                    )
                except Exception as e:
                    print(f"记录 manifest 失败: {str(e)[:100]}")
        except Exception as e:
            print(f"\n查询 [{entry.get('label', 'unknown')}] 执行出错，跳过: {str(e)[:100]}")

    progress.final_report()
    return progress


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="collection_config.json", help="Path to JSON config")
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    email = config["email"]
    if "replace-with" in email:
        raise SystemExit("Please set a project contact email in collection_config.json before collecting.")

    continuous_mode = config.get("continuous_mode", False)
    cycle_count = 0

    print(f"\n{'='*60}")
    print(f"程序启动 - 持续模式: {'开启' if continuous_mode else '关闭'}")
    print(f"{'='*60}")

    while True:
        cycle_count += 1
        print(f"\n{'#'*60}")
        print(f"第 {cycle_count} 次爬取循环开始")
        print(f"时间: {dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{'#'*60}")

        try:
            crawl_once(config)
        except Exception as e:
            print(f"\n爬取循环异常: {str(e)}")
            with open(Path(config.get("output_dir", "data")) / "crawl_progress.txt", "a", encoding="utf-8") as f:
                f.write(f"\n[{dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 爬取循环异常: {str(e)}\n")

        if not continuous_mode:
            print("\n单次爬取完成，程序退出")
            break

        print(f"\n{'='*60}")
        print(f"第 {cycle_count} 次爬取循环完成，立即开始下一次循环...")
        print(f"{'='*60}")


if __name__ == "__main__":
    main()
