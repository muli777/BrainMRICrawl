"""Download scientific papers from Sci-Hub by DOI extracted from CSV.

WARNING: Sci-Hub operates in a legal gray area in many jurisdictions.
This tool is provided for educational purposes only. Always use
institutional library access when available and comply with copyright laws.

Usage:
    python scihub_downloader.py [--csv-dir D:\\CodexProjects\\BrainMRICrawl\\csv_documents] [--doi-column DOI] [--output D:\\CodexProjects\\BrainMRICrawl\\sci_hub_data]

Features:
    - Two-phase workflow: Search first, then download
    - Phase 1: Find download URLs for all DOIs, save to doi_url_list.csv
    - Phase 2: Download PDFs using found URLs, save results to download_results.csv
    - Stop on captcha/cloudflare (don't try other mirrors)
    - Unpaywall API as legal Open Access option
    - DOI format validation
    - Skip-if-already-downloaded logic
    - Progress tracking and logging
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
from typing import Any, Dict, Iterable, Optional, Tuple

import cloudscraper
from curl_cffi import requests

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

DOI_PATTERN = re.compile(r"^10\.\d{4,9}/[-._;()/:A-Z0-9]+$", re.IGNORECASE)

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

UNPAYWALL_API = "https://api.unpaywall.org/v2/{doi}?email={email}"

DEAD_MIRRORS: set[str] = set()


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


def validate_doi(doi: str) -> bool:
    return bool(DOI_PATTERN.match(doi.strip()))


def sanitize_doi(doi: str) -> str:
    doi = doi.strip()
    for prefix in ["doi:", "DOI:", "https://doi.org/", "http://doi.org/"]:
        if doi.lower().startswith(prefix.lower()):
            doi = doi[len(prefix):]
    return doi.strip()


def extract_dois_from_csv(csv_path: Path, doi_column: str) -> Iterable[str]:
    encodings = ["utf-8-sig", "utf-8", "gbk", "latin-1"]
    
    for encoding in encodings:
        try:
            with csv_path.open("r", encoding=encoding) as f:
                reader = csv.DictReader(f)
                if reader.fieldnames is None:
                    continue
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
            return
        except (UnicodeDecodeError, UnicodeError):
            continue
    
    raise ValueError(f"无法使用以下编码读取CSV文件: {', '.join(encodings)}")


def doi_to_filename(doi: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", doi)
    if len(safe) > 200:
        safe = safe[:200]
    return f"{safe}.pdf"


def safe_write(file_path: Path, content: str, max_retries: int = 3, delay: float = 1.0) -> bool:
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
    cookies_file = output_dir / "cookies.json"
    if cookies_file.exists():
        try:
            return json.loads(cookies_file.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_cookies(output_dir: Path, cookies: Dict[str, str]) -> None:
    cookies_file = output_dir / "cookies.json"
    cookies_file.write_text(json.dumps(cookies, ensure_ascii=False), encoding="utf-8")


def extract_pdf_url_from_html(html_content: bytes, mirror: str) -> Optional[str]:
    html = html_content.decode("utf-8", errors="ignore")
    
    iframe_match = re.search(r'<iframe[^>]+src=["\']([^"\']+pdf[^"\']*)["\']', html, re.IGNORECASE)
    if iframe_match:
        pdf_url = iframe_match.group(1)
        if pdf_url.startswith("//"):
            pdf_url = "https:" + pdf_url
        elif pdf_url.startswith("/"):
            pdf_url = mirror + pdf_url
        elif not pdf_url.startswith("http"):
            pdf_url = mirror.rstrip("/") + "/" + pdf_url
        return pdf_url
    
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


def find_url_from_unpaywall(doi: str, email: str = "researcher@mailinator.com") -> Tuple[Optional[str], str]:
    """Find PDF URL from Unpaywall."""
    try:
        encoded_doi = urllib.parse.quote(doi, safe="")
        url = UNPAYWALL_API.format(doi=encoded_doi, email=email)
        response = requests.get(url, impersonate="chrome120", timeout=30)
        
        if response.status_code == 422:
            return None, "unpaywall_invalid_email"
        
        if response.status_code != 200:
            return None, f"unpaywall_status_{response.status_code}"
        
        data = response.json()
        
        if data.get("is_oa") and data.get("best_oa_location"):
            pdf_url = data["best_oa_location"].get("url_for_pdf")
            if pdf_url:
                return pdf_url, "found_unpaywall"
        return None, "unpaywall_no_oa"
    except Exception as exc:
        return None, f"unpaywall_error:{str(exc)[:30]}"


def find_url_from_scihub(doi: str, mirrors: list[str], proxy: str = "") -> Tuple[Optional[str], str]:
    """Find PDF URL from Sci-Hub mirrors. STOP on captcha/cloudflare."""
    cookies = {}
    curl_options = {"impersonate": "chrome120", "timeout": 30}
    if proxy:
        curl_options["proxy"] = proxy
    
    for mirror in mirrors:
        if mirror in DEAD_MIRRORS:
            continue
        
        try:
            url = f"{mirror}/{doi}"
            response = requests.get(url, **curl_options)
            content = response.content
            
            if response.cookies:
                cookies.update({str(k): str(v) for k, v in response.cookies.items()})
            
            if b"captcha" in content.lower():
                return url, "captcha_detected"
            
            if b"cloudflare" in content.lower():
                return url, "cloudflare_block"
            
            pdf_url = extract_pdf_url_from_html(content, mirror)
            if pdf_url:
                return pdf_url, f"found_scihub_{mirror}"
            
            content_type = response.headers.get("content-type", "")
            if "application/pdf" in content_type.lower() or content[:4] == b"%PDF":
                return url, f"found_scihub_direct_{mirror}"
        
        except Exception as exc:
            if "Could not resolve host" in str(exc):
                DEAD_MIRRORS.add(mirror)
                continue
            if "timeout" in str(exc).lower():
                continue
    
    return None, "no_url_found"


def find_download_url(doi: str, mirrors: list[str], email: str, proxy: str = "") -> Tuple[Optional[str], str]:
    """Find download URL for a DOI. Priority: Unpaywall > Sci-Hub."""
    print(f"  [Unpaywall] 查询...")
    pdf_url, status = find_url_from_unpaywall(doi, email)
    if pdf_url:
        return pdf_url, status
    if status in ["captcha_detected", "cloudflare_block"]:
        return None, status
    
    print(f"  [Sci-Hub] 查找...")
    pdf_url, status = find_url_from_scihub(doi, mirrors, proxy)
    return pdf_url, status


def phase1_find_urls(dois: list[str], output_dir: Path, mirrors: list[str], email: str, proxy: str, delay: float) -> None:
    """Phase 1: Find download URLs for all DOIs and save to CSV."""
    url_list_file = output_dir / "doi_url_list.csv"
    start_time = dt.datetime.now()
    total_count = len(dois)
    found_count = 0
    captcha_count = 0
    cloudflare_count = 0
    not_found_count = 0
    
    if url_list_file.exists():
        with url_list_file.open("r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            existing_dois = {row["doi"] for row in reader}
        print(f"  已存在URL列表文件，跳过 {len(existing_dois)} 个已处理的DOI")
    else:
        existing_dois = set()
        with url_list_file.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["doi", "download_url", "source", "status", "manual_url", "timestamp"])
            writer.writeheader()
    
    last_request = 0.0
    
    for idx, doi in enumerate(dois, start=1):
        if doi in existing_dois:
            continue
        
        print(f"\n[{idx}/{total_count}] 查找 DOI: {doi[:50]}...")
        
        wait = delay - (time.monotonic() - last_request)
        if wait > 0:
            time.sleep(wait)
        
        pdf_url, status = find_download_url(doi, mirrors, email, proxy)
        last_request = time.monotonic()
        
        manual_url = ""
        if pdf_url:
            if status in ["captcha_detected", "cloudflare_block"]:
                manual_url = pdf_url
                pdf_url = ""
                source = ""
                if status == "captcha_detected":
                    captcha_count += 1
                else:
                    cloudflare_count += 1
                print(f"  ⚠ {status}: {manual_url[:60]}")
            else:
                print(f"  ✓ 找到下载地址: {pdf_url[:60]}")
                found_count += 1
                source = "Unpaywall" if "unpaywall" in status else "Sci-Hub"
        else:
            print(f"  ✗ {status}")
            pdf_url = ""
            source = ""
            if status == "captcha_detected":
                captcha_count += 1
            elif status == "cloudflare_block":
                cloudflare_count += 1
            else:
                not_found_count += 1
        
        record = {
            "doi": doi,
            "download_url": pdf_url,
            "source": source,
            "status": status,
            "manual_url": manual_url,
            "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        
        with url_list_file.open("a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["doi", "download_url", "source", "status", "manual_url", "timestamp"])
            writer.writerow(record)
    
    elapsed = (dt.datetime.now() - start_time).total_seconds()
    print(f"\n{'#'*60}")
    print(f"阶段1完成 - URL查找")
    print(f"{'#'*60}")
    print(f"  总计DOI: {total_count}")
    print(f"  找到URL: {found_count}")
    print(f"  验证码拦截: {captcha_count}")
    print(f"  Cloudflare拦截: {cloudflare_count}")
    print(f"  未找到: {not_found_count}")
    print(f"  总耗时: {format_elapsed(elapsed)}")
    print(f"{'#'*60}")


def phase2_download(url_list_file: Path, output_dir: Path, proxy: str, delay: float, max_retries: int = 2) -> None:
    """Phase 2: Download PDFs using found URLs."""
    results_file = output_dir / "download_results.csv"
    start_time = dt.datetime.now()
    success_count = 0
    failure_count = 0
    skipped_count = 0
    captcha_count = 0
    
    if not url_list_file.exists():
        print(f"错误: URL列表文件不存在 - {url_list_file}")
        print("请先运行阶段1查找URL")
        return
    
    existing_dois = load_existing_downloads(output_dir)
    print(f"  已跳过 {len(existing_dois)} 个已下载的DOI")
    
    if results_file.exists():
        with results_file.open("r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            processed_dois = {row["doi"] for row in reader}
        print(f"  已跳过 {len(processed_dois)} 个已处理的DOI")
    else:
        processed_dois = set()
        with results_file.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["doi", "filename", "download_url", "status", "reason", "timestamp"])
            writer.writeheader()
    
    with url_list_file.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        url_records = list(reader)
    
    total_count = len(url_records)
    last_request = 0.0
    
    for idx, record in enumerate(url_records, start=1):
        doi = record["doi"]
        download_url = record.get("download_url", "")
        status = record.get("status", "")
        
        if doi in existing_dois:
            skipped_count += 1
            continue
        
        if doi in processed_dois:
            skipped_count += 1
            continue
        
        if status in ["captcha_detected", "cloudflare_block"]:
            print(f"\n[{idx}/{total_count}] DOI: {doi[:50]} - 验证拦截，跳过")
            skipped_count += 1
            continue
        
        if not download_url:
            print(f"\n[{idx}/{total_count}] DOI: {doi[:50]} - 无下载地址，跳过")
            skipped_count += 1
            continue
        
        print(f"\n[{idx}/{total_count}] 下载 DOI: {doi[:50]}...")
        
        wait = delay - (time.monotonic() - last_request)
        if wait > 0:
            time.sleep(wait)
        
        filename = doi_to_filename(doi)
        destination = output_dir / filename
        
        download_success = False
        download_reason = ""
        
        for attempt in range(max_retries):
            try:
                print(f"  尝试 ({attempt+1}/{max_retries}): {download_url[:60]}...")
                
                response = requests.get(download_url, impersonate="chrome120", timeout=60)
                
                if b"captcha" in response.content.lower() or b"cloudflare" in response.content.lower():
                    download_reason = "captcha_or_cloudflare"
                    captcha_count += 1
                    break
                
                if response.content[:4] == b"%PDF":
                    destination.write_bytes(response.content)
                    download_success = True
                    download_reason = "downloaded_successfully"
                    success_count += 1
                    print(f"  ✓ 下载成功")
                    break
                else:
                    download_reason = "content_not_pdf"
                    print(f"  内容不是PDF")
            
            except Exception as exc:
                download_reason = f"download_error:{str(exc)[:30]}"
                print(f"  下载异常: {str(exc)[:50]}")
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt * 2)
        
        if not download_success:
            failure_count += 1
            print(f"  ✗ 下载失败: {download_reason}")
        
        last_request = time.monotonic()
        
        result_record = {
            "doi": doi,
            "filename": filename if download_success else "",
            "download_url": download_url,
            "status": "success" if download_success else "failure",
            "reason": download_reason,
            "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        
        with results_file.open("a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["doi", "filename", "download_url", "status", "reason", "timestamp"])
            writer.writerow(result_record)
    
    elapsed = (dt.datetime.now() - start_time).total_seconds()
    print(f"\n{'#'*60}")
    print(f"阶段2完成 - PDF下载")
    print(f"{'#'*60}")
    print(f"  总计URL: {total_count}")
    print(f"  下载成功: {success_count}")
    print(f"  下载失败: {failure_count}")
    print(f"  已跳过: {skipped_count}")
    print(f"  验证拦截: {captcha_count}")
    print(f"  总耗时: {format_elapsed(elapsed)}")
    print(f"{'#'*60}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--csv-dir", default=r"C:\Users\16701\Desktop\naoxueguan", help="CSV文件所在目录")
    parser.add_argument("--doi-column", default="DOI", help="DOI所在的列名")
    parser.add_argument("--output", default=r"C:\Users\16701\Desktop\naoxueguan\paper", help="PDF输出目录")
    parser.add_argument("--mirrors", nargs="+", default=SCIHUB_MIRRORS, help="Sci-Hub镜像地址列表")
    parser.add_argument("--delay", type=float, default=2.0, help="请求间隔时间(秒)")
    parser.add_argument("--email", default="researcher@mailinator.com", help="Unpaywall API邮箱地址")
    parser.add_argument("--proxy", default="", help="HTTP/SOCKS5代理地址")
    parser.add_argument("--phase", type=int, choices=[1, 2, 0], default=0, 
                        help="运行阶段: 1=仅查找URL, 2=仅下载, 0=先查找再下载")
    args = parser.parse_args()

    csv_dir = Path(args.csv_dir)
    output_dir = Path(args.output)
    
    if not csv_dir.exists():
        raise SystemExit(f"错误: CSV目录不存在 - {csv_dir}")
    
    output_dir.mkdir(parents=True, exist_ok=True)
    
    print(f"\n{'='*60}")
    print("Sci-Hub DOI下载器 (两阶段模式)")
    print(f"{'='*60}")
    print(f"CSV目录: {csv_dir}")
    print(f"DOI列: {args.doi_column}")
    print(f"输出目录: {output_dir}")
    print(f"运行阶段: {'阶段1+阶段2' if args.phase == 0 else f'阶段{args.phase}'}")
    print(f"{'='*60}")

    csv_files = sorted(csv_dir.glob("*.csv"))
    if not csv_files:
        raise SystemExit(f"错误: 在目录 {csv_dir} 中未找到CSV文件")
    
    print(f"找到 {len(csv_files)} 个CSV文件:")
    for csv_file in csv_files:
        print(f"  - {csv_file.name}")

    dois = []
    for csv_file in csv_files:
        try:
            file_dois = list(extract_dois_from_csv(csv_file, args.doi_column))
            dois.extend(file_dois)
            print(f"从 {csv_file.name} 提取到 {len(file_dois)} 个有效DOI")
        except Exception as e:
            print(f"读取 {csv_file.name} 失败: {str(e)}")

    dois = list(dict.fromkeys(dois))
    print(f"去重后共 {len(dois)} 个有效DOI")

    if not dois:
        print("没有找到有效的DOI，程序退出")
        return

    url_list_file = output_dir / "doi_url_list.csv"
    
    if args.phase == 1 or args.phase == 0:
        print(f"\n{'='*60}")
        print("阶段1: 查找下载URL")
        print(f"{'='*60}")
        phase1_find_urls(dois, output_dir, args.mirrors, args.email, args.proxy, args.delay)
    
    if args.phase == 2 or args.phase == 0:
        print(f"\n{'='*60}")
        print("阶段2: 下载PDF")
        print(f"{'='*60}")
        phase2_download(url_list_file, output_dir, args.proxy, args.delay)
    
    print(f"\n所有阶段完成！结果保存在: {output_dir}")


if __name__ == "__main__":
    main()