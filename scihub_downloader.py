"""Download scientific papers from Sci-Hub by DOI extracted from CSV.

WARNING: Sci-Hub operates in a legal gray area in many jurisdictions.
This tool is provided for educational purposes only. Always use
institutional library access when available and comply with copyright laws.

Usage:
    python scihub_downloader.py [--csv-dir D:\\CodexProjects\\BrainMRICrawl\\csv_documents] [--doi-column DOI] [--output D:\\CodexProjects\\BrainMRICrawl\\sci_hub_data]

Features:
    - Extracts DOIs from all CSV files in a directory
    - Configurable Sci-Hub mirror URLs with fallback
    - Cloudflare bypass using cloudscraper
    - Cookie persistence for reduced Cloudflare challenges
    - Unpaywall API as legal Open Access fallback
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
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

import cloudscraper
from curl_cffi import requests

# Default Sci-Hub mirrors (may need updating as domains get blocked)
# Ordered by reliability based on testing
SCIHUB_MIRRORS = [
    "https://sci-hub.se",
    "https://sci-hub.ru",
    "https://sci-hub.st",
    "https://sci-hub.mksa.top",
    "https://sci-hub.ren",
    "https://sci-hub.ru.hr",
    "https://sci-hub.tw",
    "https://sci-hub.ee",
]

# DOI regex pattern for validation
DOI_PATTERN = re.compile(r"^10\.\d{4,9}/[-._;()/:A-Z0-9]+$", re.IGNORECASE)

# Default user agent
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# Unpaywall API (legal Open Access fallback)
UNPAYWALL_API = "https://api.unpaywall.org/v2/{doi}?email={email}"

# Global cache for dead mirrors (DNS resolution failures)
DEAD_MIRRORS: set[str] = set()


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
    safe_write(path, json.dumps(record, ensure_ascii=False) + "\n")


def safe_write(file_path: Path, content: str, max_retries: int = 3, delay: float = 1.0) -> bool:
    """Safely write content to file with retry on PermissionError."""
    for attempt in range(max_retries):
        try:
            with file_path.open("a", encoding="utf-8") as f:
                f.write(content)
            return True
        except PermissionError:
            if attempt < max_retries - 1:
                time.sleep(delay)
            else:
                print(f"  警告: 无法写入文件 {file_path}，权限被拒绝")
                return False
        except Exception as e:
            print(f"  警告: 写入文件失败: {str(e)}")
            return False


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


def load_cookies(output_dir: Path) -> Dict[str, str]:
    """Load saved cookies from file."""
    cookies_file = output_dir / "cookies.json"
    if cookies_file.exists():
        try:
            return json.loads(cookies_file.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_cookies(output_dir: Path, cookies: Dict[str, str]) -> None:
    """Save cookies to file."""
    cookies_file = output_dir / "cookies.json"
    cookies_file.write_text(json.dumps(cookies, ensure_ascii=False), encoding="utf-8")


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


def download_from_unpaywall(doi: str, output_dir: Path, email: str = "researcher@mailinator.com") -> tuple[bool, str]:
    """Try to download PDF from Unpaywall (legal Open Access)."""
    filename = doi_to_filename(doi)
    destination = output_dir / filename
    
    if destination.exists():
        return True, "already_exists"
    
    try:
        # URL encode the DOI to handle special characters
        encoded_doi = urllib.parse.quote(doi, safe="")
        url = UNPAYWALL_API.format(doi=encoded_doi, email=email)
        print(f"  查询Unpaywall: {url[:80]}...")
        
        # Use curl_cffi for better SSL handling
        response = requests.get(url, impersonate="chrome120", timeout=30)
        
        if response.status_code == 422:
            print("  Unpaywall拒绝了该邮箱地址，请使用--email参数提供其他邮箱")
            return False, "unpaywall_invalid_email"
        
        if response.status_code != 200:
            print(f"  Unpaywall返回错误: {response.status_code}")
            return False, f"unpaywall_status_{response.status_code}"
        
        data = response.json()
        
        if data.get("is_oa") and data.get("best_oa_location"):
            pdf_url = data["best_oa_location"].get("url_for_pdf")
            if pdf_url:
                print(f"  尝试从Unpaywall下载: {pdf_url[:60]}...")
                pdf_response = requests.get(pdf_url, impersonate="chrome120", timeout=60)
                if pdf_response.status_code == 200 and pdf_response.content[:4] == b"%PDF":
                    destination.write_bytes(pdf_response.content)
                    return True, "downloaded_from_unpaywall"
        return False, "unpaywall_no_oa"
    except Exception as exc:
        print(f"  Unpaywall请求失败: {str(exc)[:50]}")
        return False, f"unpaywall_error:{str(exc)[:30]}"


def download_pdf(doi: str, output_dir: Path, mirrors: list[str], max_retries: int = 3, email: str = "researcher@mailinator.com", proxy: str = "") -> tuple[bool, str]:
    """Download PDF from Sci-Hub with retry and mirror fallback using curl_cffi."""
    filename = doi_to_filename(doi)
    destination = output_dir / filename
    
    if destination.exists():
        return True, "already_exists"
    
    # Load saved cookies
    cookies = load_cookies(output_dir)
    
    # Build curl_cffi options
    curl_options = {"impersonate": "chrome120", "timeout": 60, "cookies": cookies}
    if proxy:
        curl_options["proxy"] = proxy
    
    for mirror in mirrors:
        # Skip dead mirrors (DNS resolution failures)
        if mirror in DEAD_MIRRORS:
            print(f"  跳过已标记为不可用的镜像: {mirror[:30]}")
            continue
        
        for attempt in range(max_retries):
            try:
                # First try with curl_cffi impersonating Chrome
                url = f"{mirror}/{doi}"
                print(f"  尝试: {url[:60]}...")
                
                response = requests.get(url, **curl_options)
                content = response.content
                
                # Save cookies for reuse
                if response.cookies:
                    new_cookies = {str(k): str(v) for k, v in response.cookies.items()}
                    cookies.update(new_cookies)
                    save_cookies(output_dir, cookies)
                
                # Check if response is PDF directly
                content_type = response.headers.get("content-type", "")
                if "application/pdf" in content_type.lower() or content[:4] == b"%PDF":
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
                    pdf_response = requests.get(pdf_url, impersonate="chrome120", timeout=60)
                    pdf_content = pdf_response.content
                    if pdf_content[:4] == b"%PDF":
                        destination.write_bytes(pdf_content)
                        return True, f"downloaded_via_html_from_{mirror}"
                    else:
                        print(f"  获取的内容不是PDF，尝试其他方式...")
            
            except Exception as exc:
                print(f"  下载异常: {str(exc)[:50]}")
                # Mark mirror as dead if DNS resolution fails
                if "Could not resolve host" in str(exc):
                    DEAD_MIRRORS.add(mirror)
                    print(f"  标记镜像 {mirror} 为不可用")
                    break  # Skip to next mirror immediately
                if attempt < max_retries - 1:
                    wait = 2 ** attempt * 3
                    print(f"  等待 {wait} 秒后重试...")
                    time.sleep(wait)
                else:
                    print(f"  尝试下一个镜像...")
    
    # Try Unpaywall first (legal and DNS-unblocked)
    print(f"  尝试Unpaywall (开放获取)...")
    success, status = download_from_unpaywall(doi, output_dir, email)
    if success:
        return True, status
    
    # Try with cloudscraper
    print(f"  Unpaywall无开放获取版本，尝试cloudscraper...")
    for mirror in mirrors:
        if mirror in DEAD_MIRRORS:
            continue
        
        for attempt in range(max_retries):
            try:
                scraper = cloudscraper.create_scraper()
                url = f"{mirror}/{doi}"
                
                scraper_kwargs = {"timeout": 60}
                if proxy:
                    scraper_kwargs["proxies"] = {"http": proxy, "https": proxy}
                
                response = scraper.get(url, **scraper_kwargs)
                content = response.content
                
                if "application/pdf" in response.headers.get("Content-Type", "") or content[:4] == b"%PDF":
                    destination.write_bytes(content)
                    return True, f"downloaded_cloudscraper_from_{mirror}"
                
                pdf_url = extract_pdf_url_from_html(content, mirror)
                if pdf_url:
                    pdf_response = scraper.get(pdf_url, timeout=60)
                    pdf_content = pdf_response.content
                    if pdf_content[:4] == b"%PDF":
                        destination.write_bytes(pdf_content)
                        return True, f"downloaded_cloudscraper_html_from_{mirror}"
            
            except Exception as exc:
                print(f"  cloudscraper异常: {str(exc)[:30]}")
                if "Could not resolve host" in str(exc):
                    DEAD_MIRRORS.add(mirror)
                    break
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt * 2)
    
    return False, "all_mirrors_failed"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--csv-dir", default=r"D:\CodexProjects\BrainMRICrawl\csv_documents", help="CSV文件所在目录 (默认: D:\\CodexProjects\\BrainMRICrawl\\csv_documents)")
    parser.add_argument("--doi-column", default="DOI", help="DOI所在的列名 (默认: DOI)")
    parser.add_argument("--output", default=r"D:\CodexProjects\BrainMRICrawl\sci_hub_data", help="PDF输出目录 (默认: D:\\CodexProjects\\BrainMRICrawl\\sci_hub_data)")
    parser.add_argument("--mirrors", nargs="+", default=SCIHUB_MIRRORS, help="Sci-Hub镜像地址列表")
    parser.add_argument("--max-retries", type=int, default=3, help="每个镜像的最大重试次数")
    parser.add_argument("--delay", type=float, default=2.0, help="下载间隔时间(秒)")
    parser.add_argument("--email", default="researcher@mailinator.com", help="Unpaywall API邮箱地址（格式正确即可，无需真实邮箱）")
    parser.add_argument("--proxy", default="", help="HTTP/SOCKS5代理地址，如: http://proxy.example.com:8080 或 socks5://localhost:1080")
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
    
    # CSV output files (fixed names, append mode)
    success_csv = output_dir / "download_success.csv"
    failure_csv = output_dir / "download_failure.csv"
    
    # Initialize CSV files with headers if they don't exist
    if not success_csv.exists():
        for attempt in range(3):
            try:
                with success_csv.open("w", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(f, fieldnames=["doi", "filename", "reason", "timestamp"])
                    writer.writeheader()
                break
            except PermissionError:
                if attempt < 2:
                    time.sleep(1)
    
    if not failure_csv.exists():
        for attempt in range(3):
            try:
                with failure_csv.open("w", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(f, fieldnames=["doi", "reason", "timestamp"])
                    writer.writeheader()
                break
            except PermissionError:
                if attempt < 2:
                    time.sleep(1)

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
        success, status = download_pdf(doi, output_dir, args.mirrors, args.max_retries, args.email, args.proxy)
        last_request = time.monotonic()
        
        if success:
            print(f"  ✓ 下载成功")
            progress.increment("success")
            filename = doi_to_filename(doi)
            record = {
                "doi": doi,
                "filename": filename,
                "reason": status,
                "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
            }
            # Append to success CSV with retry
            for attempt in range(3):
                try:
                    with success_csv.open("a", newline="", encoding="utf-8") as f:
                        writer = csv.DictWriter(f, fieldnames=["doi", "filename", "reason", "timestamp"])
                        writer.writerow(record)
                    break
                except PermissionError:
                    if attempt < 2:
                        time.sleep(1)
                    else:
                        print(f"  警告: 无法写入 success CSV")
        else:
            print(f"  ✗ 下载失败: {status}")
            progress.increment("failure")
            record = {
                "doi": doi,
                "reason": status,
                "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
            }
            # Append to failure CSV with retry
            for attempt in range(3):
                try:
                    with failure_csv.open("a", newline="", encoding="utf-8") as f:
                        writer = csv.DictWriter(f, fieldnames=["doi", "reason", "timestamp"])
                        writer.writerow(record)
                    break
                except PermissionError:
                    if attempt < 2:
                        time.sleep(1)
                    else:
                        print(f"  警告: 无法写入 failure CSV")
        
        # Record in manifest
        append_manifest(manifest_file, {
            "doi": doi,
            "status": "success" if success else "failure",
            "reason": status,
            "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
            "filename": doi_to_filename(doi) if success else None,
        })

    print(f"\n成功记录已保存到: {success_csv}")
    print(f"失败记录已保存到: {failure_csv}")
    
    progress.final_report()
    print(f"\n下载完成！PDF文件保存在: {output_dir}")


if __name__ == "__main__":
    main()
