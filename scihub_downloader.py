"""Download scientific papers from Sci-Hub by DOI extracted from CSV.

WARNING: Sci-Hub operates in a legal gray area in many jurisdictions.
This tool is provided for educational purposes only. Always use
institutional library access when available and comply with copyright laws.

Usage:
    python scihub_downloader.py [--csv-dir D:\\CodexProjects\\BrainMRICrawl\\csv_documents] [--doi-column DOI] [--output D:\\CodexProjects\\BrainMRICrawl\\sci_hub_data]

Features:
    - Extracts DOIs from all CSV files in a directory
    - Configurable Sci-Hub mirror URLs with fallback
    - DOI format validation
    - Skip-if-already-downloaded logic
    - Download retry with exponential backoff
    - Progress tracking and manifest logging
    - Robust error handling
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable, Optional

# Default Sci-Hub mirrors (may need updating as domains get blocked)
# Ordered by reliability based on testing
SCIHUB_MIRRORS = [
    "https://sci-hub.mksa.top",
    "https://sci-hub.se",
    "https://sci-hub.ru",
    "https://sci-hub.st",
    "https://sci-hub.ren",
]

# DOI regex pattern for validation
DOI_PATTERN = re.compile(r"^10\.\d{4,9}/[-._;()/:A-Z0-9]+$", re.IGNORECASE)

# Default user agent
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


def format_elapsed(seconds: float) -> str:
    """Format elapsed time into human-readable string."""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    if hours > 0:
        return f"{hours}小时{minutes}分钟{secs:.2f}秒"
    elif minutes > 0:
        return f"{minutes}分钟{secs:.2f}秒"
    else:
        return f"{secs:.2f}秒"


class DownloadProgress:
    """Track download progress and generate reports."""

    def __init__(self, log_file: Path) -> None:
        self.log_file = log_file
        self.total_count = 0
        self.success_count = 0
        self.failure_count = 0
        self.skipped_count = 0
        self.start_time = dt.datetime.now()

    def start(self) -> None:
        """Start tracking progress."""
        message = f"开始下载任务 - {self.start_time.strftime('%Y-%m-%d %H:%M:%S')}"
        print(f"\n{'='*60}")
        print(message)
        print(f"{'='*60}")
        with self.log_file.open("a", encoding="utf-8") as f:
            f.write(f"{message}\n")

    def increment(self, status: str) -> None:
        """Increment counter for given status."""
        self.total_count += 1
        if status == "success":
            self.success_count += 1
        elif status == "failure":
            self.failure_count += 1
        elif status == "skipped":
            self.skipped_count += 1

    def final_report(self) -> None:
        """Print and write final progress report."""
        elapsed = (dt.datetime.now() - self.start_time).total_seconds()
        print(f"\n{'#'*60}")
        print(f"最终汇总报告")
        print(f"{'#'*60}")
        print(f"  总计DOI: {self.total_count}")
        print(f"  成功下载: {self.success_count}")
        print(f"  下载失败: {self.failure_count}")
        print(f"  已跳过: {self.skipped_count}")
        print(f"  成功率: {(self.success_count / self.total_count * 100):.2f}%")
        print(f"  总耗时: {format_elapsed(elapsed)}")
        print(f"  结束时间: {dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{'#'*60}")

        with self.log_file.open("a", encoding="utf-8") as f:
            f.write(f"\n{'#'*60}\n")
            f.write(f"最终汇总报告\n")
            f.write(f"时间: {dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"{'#'*60}\n")
            f.write(f"  总计DOI: {self.total_count}\n")
            f.write(f"  成功下载: {self.success_count}\n")
            f.write(f"  下载失败: {self.failure_count}\n")
            f.write(f"  已跳过: {self.skipped_count}\n")
            f.write(f"  成功率: {(self.success_count / self.total_count * 100):.2f}%\n")
            f.write(f"  总耗时: {format_elapsed(elapsed)}\n")
            f.write(f"{'#'*60}\n")


def validate_doi(doi: str) -> bool:
    """Validate DOI format."""
    return bool(DOI_PATTERN.match(doi.strip()))


def sanitize_doi(doi: str) -> str:
    """Clean up DOI string - remove whitespace and prefixes."""
    doi = doi.strip()
    # Remove common DOI prefixes
    for prefix in ["doi:", "DOI:", "https://doi.org/", "http://doi.org/"]:
        if doi.lower().startswith(prefix.lower()):
            doi = doi[len(prefix):]
    return doi.strip()


def extract_dois_from_csv(csv_path: Path, doi_column: str) -> Iterable[str]:
    """Extract DOIs from CSV file with encoding fallback."""
    encodings = ["utf-8-sig", "utf-8", "gbk", "latin-1"]
    
    for encoding in encodings:
        try:
            with csv_path.open("r", encoding=encoding) as f:
                reader = csv.DictReader(f)
                if reader.fieldnames is None:
                    continue
                # Case-insensitive column matching
                fieldnames_lower = {name.lower(): name for name in reader.fieldnames}
                actual_column = fieldnames_lower.get(doi_column.lower())
                
                if not actual_column:
                    raise ValueError(f"CSV文件中未找到列 '{doi_column}'。可用列: {', '.join(reader.fieldnames)}")
                
                for row_num, row in enumerate(reader, start=2):
                    doi_value = row.get(actual_column, "").strip()
                    if doi_value:
                        sanitized = sanitize_doi(doi_value)
                        if validate_doi(sanitized):
                            yield sanitized
                        else:
                            print(f"警告: 第 {row_num} 行的DOI格式无效，已跳过: {doi_value}")
            return  # Successfully read with this encoding
        except (UnicodeDecodeError, UnicodeError):
            continue
    
    raise ValueError(f"无法使用以下编码读取CSV文件: {', '.join(encodings)}")


def doi_to_filename(doi: str) -> str:
    """Convert DOI to safe filename."""
    # Replace invalid filename characters
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", doi)
    # Limit length to avoid filesystem issues
    if len(safe) > 200:
        safe = safe[:200]
    return f"{safe}.pdf"


def append_manifest(path: Path, record: dict[str, Any]) -> None:
    """Append record to manifest JSONL file."""
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_existing_downloads(output_dir: Path) -> set[str]:
    """Load already downloaded DOIs from manifest."""
    existing = set()
    manifest_path = output_dir / "download_manifest.jsonl"
    if manifest_path.exists():
        with manifest_path.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    record = json.loads(line)
                    if record.get("status") == "success" and record.get("doi"):
                        existing.add(record["doi"])
                except json.JSONDecodeError:
                    continue
    return existing


def extract_pdf_url_from_html(html_content: bytes, mirror: str) -> Optional[str]:
    """Extract PDF download URL from Sci-Hub HTML page."""
    html = html_content.decode("utf-8", errors="ignore")
    
    # Pattern 1: iframe with src containing pdf
    iframe_match = re.search(r'<iframe[^>]+src=["\']([^"\']+pdf[^"\']*)["\']', html, re.IGNORECASE)
    if iframe_match:
        pdf_url = iframe_match.group(1)
        # Handle relative URLs
        if pdf_url.startswith("//"):
            pdf_url = "https:" + pdf_url
        elif pdf_url.startswith("/"):
            pdf_url = mirror + pdf_url
        elif not pdf_url.startswith("http"):
            pdf_url = mirror.rstrip("/") + "/" + pdf_url
        return pdf_url
    
    # Pattern 2: embed with src containing pdf
    embed_match = re.search(r'<embed[^>]+src=["\']([^"\']+pdf[^"\']*)["\']', html, re.IGNORECASE)
    if embed_match:
        pdf_url = embed_match.group(1)
        if pdf_url.startswith("//"):
            pdf_url = "https:" + pdf_url
        elif pdf_url.startswith("/"):
            pdf_url = mirror + pdf_url
        elif not pdf_url.startswith("http"):
            pdf_url = mirror.rstrip("/") + "/" + pdf_url
        return pdf_url
    
    # Pattern 3: link with onclick containing pdf
    onclick_match = re.search(r'onclick=["\']location\.href=["\']([^"\']+pdf[^"\']*)["\']', html, re.IGNORECASE)
    if onclick_match:
        pdf_url = onclick_match.group(1)
        if pdf_url.startswith("//"):
            pdf_url = "https:" + pdf_url
        elif pdf_url.startswith("/"):
            pdf_url = mirror + pdf_url
        elif not pdf_url.startswith("http"):
            pdf_url = mirror.rstrip("/") + "/" + pdf_url
        return pdf_url
    
    # Pattern 4: direct link to pdf
    link_match = re.search(r'<a[^>]+href=["\']([^"\']+pdf[^"\']*)["\']', html, re.IGNORECASE)
    if link_match:
        pdf_url = link_match.group(1)
        if pdf_url.startswith("//"):
            pdf_url = "https:" + pdf_url
        elif pdf_url.startswith("/"):
            pdf_url = mirror + pdf_url
        elif not pdf_url.startswith("http"):
            pdf_url = mirror.rstrip("/") + "/" + pdf_url
        return pdf_url
    
    return None


def download_pdf(doi: str, output_dir: Path, mirrors: list[str], max_retries: int = 3) -> tuple[bool, str]:
    """Download PDF from Sci-Hub with retry and mirror fallback."""
    filename = doi_to_filename(doi)
    destination = output_dir / filename
    
    if destination.exists():
        return True, "already_exists"
    
    for mirror in mirrors:
        for attempt in range(max_retries):
            try:
                # First try direct download URL
                url = f"{mirror}/{doi}"
                request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                
                with urllib.request.urlopen(request, timeout=60) as response:
                    content_type = response.headers.get("Content-Type", "")
                    content = response.read()
                    
                    # Check if response is PDF directly
                    if "application/pdf" in content_type or content[:4] == b"%PDF":
                        destination.write_bytes(content)
                        return True, f"downloaded_directly_from_{mirror}"
                    
                    # Check for captcha or cloudflare
                    if b"captcha" in content.lower() or b"cloudflare" in content.lower():
                        print(f"  检测到验证码/Cloudflare，尝试下一个镜像...")
                        break
                    
                    # Try to extract PDF URL from HTML
                    pdf_url = extract_pdf_url_from_html(content, mirror)
                    if pdf_url:
                        print(f"  找到PDF链接: {pdf_url[:60]}...")
                        pdf_request = urllib.request.Request(pdf_url, headers={"User-Agent": USER_AGENT})
                        with urllib.request.urlopen(pdf_request, timeout=60) as pdf_response:
                            pdf_content = pdf_response.read()
                            if pdf_content[:4] == b"%PDF":
                                destination.write_bytes(pdf_content)
                                return True, f"downloaded_via_html_from_{mirror}"
                            else:
                                print(f"  获取的内容不是PDF，尝试其他方式...")
                
                # Try alternative URL formats
                for alt_format in ["/downloads/{doi}.pdf", "/doi/{doi}"]:
                    alt_url = mirror.rstrip("/") + alt_format.format(doi=doi)
                    try:
                        alt_request = urllib.request.Request(alt_url, headers={"User-Agent": USER_AGENT})
                        with urllib.request.urlopen(alt_request, timeout=60) as alt_response:
                            alt_content = alt_response.read()
                            if alt_content[:4] == b"%PDF":
                                destination.write_bytes(alt_content)
                                return True, f"downloaded_alt_format_from_{mirror}"
                    except urllib.error.HTTPError:
                        continue
                    except Exception:
                        continue
                
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    break  # Move to next mirror
                elif exc.code == 429:
                    wait = 2 ** attempt * 5
                    print(f"  请求被限制，等待 {wait} 秒后重试...")
                    time.sleep(wait)
                    continue
                else:
                    print(f"  HTTP错误 {exc.code}，尝试下一个镜像...")
                    break
            except urllib.error.URLError as exc:
                print(f"  网络错误: {exc.reason}，尝试下一个镜像...")
                break
            except Exception as exc:
                print(f"  下载异常: {str(exc)[:50]}")
                if attempt < max_retries - 1:
                    wait = 2 ** attempt * 3
                    print(f"  等待 {wait} 秒后重试...")
                    time.sleep(wait)
    
    return False, "all_mirrors_failed"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--csv-dir", default=r"D:\CodexProjects\BrainMRICrawl\csv_documents", help="CSV文件所在目录 (默认: D:\\CodexProjects\\BrainMRICrawl\\csv_documents)")
    parser.add_argument("--doi-column", default="DOI", help="DOI所在的列名 (默认: DOI)")
    parser.add_argument("--output", default=r"D:\CodexProjects\BrainMRICrawl\sci_hub_data", help="PDF输出目录 (默认: D:\\CodexProjects\\BrainMRICrawl\\sci_hub_data)")
    parser.add_argument("--mirrors", nargs="+", default=SCIHUB_MIRRORS, help="Sci-Hub镜像地址列表")
    parser.add_argument("--max-retries", type=int, default=3, help="每个镜像的最大重试次数")
    parser.add_argument("--delay", type=float, default=2.0, help="下载间隔时间(秒)")
    args = parser.parse_args()

    csv_dir = Path(args.csv_dir)
    output_dir = Path(args.output)
    
    if not csv_dir.exists():
        raise SystemExit(f"错误: CSV目录不存在 - {csv_dir}")
    
    output_dir.mkdir(parents=True, exist_ok=True)
    
    log_file = output_dir / "download_progress.txt"
    manifest_file = output_dir / "download_manifest.jsonl"
    
    print(f"\n{'='*60}")
    print("Sci-Hub DOI下载器")
    print(f"{'='*60}")
    print(f"CSV目录: {csv_dir}")
    print(f"DOI列: {args.doi_column}")
    print(f"输出目录: {output_dir}")
    print(f"使用镜像: {len(args.mirrors)} 个")
    print(f"{'='*60}")

    # Find all CSV files in directory
    csv_files = sorted(csv_dir.glob("*.csv"))
    if not csv_files:
        raise SystemExit(f"错误: 在目录 {csv_dir} 中未找到CSV文件")
    
    print(f"找到 {len(csv_files)} 个CSV文件:")
    for csv_file in csv_files:
        print(f"  - {csv_file.name}")

    # Load already downloaded DOIs
    existing_dois = load_existing_downloads(output_dir)
    print(f"\n已加载 {len(existing_dois)} 条已下载记录")

    # Extract DOIs from all CSV files
    dois = []
    for csv_file in csv_files:
        try:
            file_dois = list(extract_dois_from_csv(csv_file, args.doi_column))
            dois.extend(file_dois)
            print(f"从 {csv_file.name} 提取到 {len(file_dois)} 个有效DOI")
        except Exception as e:
            print(f"读取 {csv_file.name} 失败: {str(e)}")

    # Remove duplicates
    dois = list(dict.fromkeys(dois))
    print(f"去重后共 {len(dois)} 个有效DOI")

    if not dois:
        print("没有找到有效的DOI，程序退出")
        return

    # Start downloading
    progress = DownloadProgress(log_file)
    progress.start()
    
    last_request = 0.0

    for idx, doi in enumerate(dois, start=1):
        print(f"\n[{idx}/{len(dois)}] 处理 DOI: {doi[:50]}...")
        
        # Skip already downloaded
        if doi in existing_dois:
            print(f"  已下载，跳过")
            progress.increment("skipped")
            append_manifest(manifest_file, {
                "doi": doi,
                "status": "skipped",
                "reason": "already_downloaded",
                "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
            })
            continue

        # Rate limiting
        wait = args.delay - (time.monotonic() - last_request)
        if wait > 0:
            time.sleep(wait)
        
        # Download
        success, status = download_pdf(doi, output_dir, args.mirrors, args.max_retries)
        last_request = time.monotonic()
        
        if success:
            print(f"  ✓ 下载成功")
            progress.increment("success")
        else:
            print(f"  ✗ 下载失败: {status}")
            progress.increment("failure")
        
        # Record in manifest
        append_manifest(manifest_file, {
            "doi": doi,
            "status": "success" if success else "failure",
            "reason": status,
            "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
            "filename": doi_to_filename(doi) if success else None,
        })

    progress.final_report()
    print(f"\n下载完成！PDF文件保存在: {output_dir}")


if __name__ == "__main__":
    main()
