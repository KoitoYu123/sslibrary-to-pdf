#!/usr/bin/env python3
"""超星汇雅图像阅读器：输入阅读网址，下载声明的全部页面并合并 PDF。"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import hashlib
import errno
import http.client
import html
import http.cookiejar
import io
import json
import math
import os
from pathlib import Path
import re
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    import img2pdf
    from PIL import Image
    from pypdf import PdfReader
except ImportError as exc:
    print(f"缺少依赖：{exc.name}\n请先运行：python -m pip install -r requirements.txt")
    raise SystemExit(1)


PARTS = [("cov", "封面"), ("bok", "书名页"), ("leg", "版权页"),
         ("fow", "前言"), ("!", "目录"), ("", "正文"),
         ("att", "附录"), ("cov", "封底")]
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36"
DEFAULT_WORKERS = 3
DEFAULT_DELAY = 0.35


class DownloadError(Exception):
    pass


class AccessError(DownloadError):
    pass


class RestartRequested(Exception):
    pass


class UserExit(Exception):
    pass


def explain_error(exc, stage="下载"):
    chain = []
    current = exc
    while current is not None and current not in chain:
        chain.append(current)
        current = current.__cause__ or current.__context__
    message = str(exc)
    if isinstance(exc, KeyboardInterrupt):
        reason, solution = "收到了 Ctrl+C 或终端中断信号。", "可选择重新开始以继续下载；再次按 Ctrl+C 可退出，缓存仍保留。"
    elif any(isinstance(e, OSError) and e.errno == errno.ENOSPC for e in chain):
        reason, solution = "磁盘空间不足。", "释放目标磁盘空间后重试，已保存图片不会删除。"
    elif any(isinstance(e, PermissionError) for e in chain):
        reason, solution = "目录没有写入权限，或目标文件正被其他程序占用。", "关闭占用 PDF 的程序，检查目录权限；重新开始时可修改保存位置。"
    elif any(isinstance(e, FileNotFoundError) for e in chain):
        reason, solution = "文件或目录不存在，例如指定的 cookies 文件。", "检查文件路径，以及外接磁盘、网络目录是否仍可用。"
    elif '429' in message:
        reason, solution = "请求频率受到服务器限制。", "等待一段时间，再重试；可降低并发数、增加请求间隔。"
    elif any(isinstance(e, AccessError) for e in chain):
        reason, solution = "登录已过期、访问被拒绝、链接失效，或服务器要求验证码。", "先在浏览器恢复正常阅读，再在重新开始时粘贴新的阅读网址；需要登录时更新自己的 cookies 文件。"
    elif any(isinstance(e, ssl.SSLError) or isinstance(getattr(e, 'reason', None), ssl.SSLError) for e in chain):
        reason, solution = "HTTPS 证书校验或加密连接失败。", "检查系统时间、网络代理和证书环境后重试。"
    elif any(isinstance(e, (urllib.error.URLError, TimeoutError, http.client.HTTPException)) for e in chain):
        reason, solution = "网络断开、连接超时或服务器传输中断。", "确认网络恢复，重试当前页或重新开始；有效缓存会被保留。"
    elif '404' in message:
        reason, solution = "这一页不存在，或当前图片路径已失效。", "检查网页上该页能否打开；可重试、跳过此页，或换新网址重新开始。"
    elif '图片' in message or '占位' in message:
        reason, solution = "图片损坏、返回了占位图，或链接失效。", "在浏览器检查对应页，恢复正常阅读后重试；必要时更新阅读网址。"
    elif stage == "合并 PDF":
        reason, solution = "图片格式、文件占用、磁盘空间或 PDF 合并过程出现问题。", "检查剩余空间、关闭目标 PDF 后重新开始；已下载图片会直接复用。"
    else:
        reason, solution = "阅读网址不适用、页面结构变化、服务器故障，或程序遇到异常。", "核对终端错误详情，检查图像版阅读网址、网络和保存路径，再选择重新开始。"
    print(f"\n【{stage}未完成】{type(exc).__name__}：{message}\n可能原因：{reason}\n解决办法：{solution}", flush=True)


def ask_choice(prompt, choices, default):
    while True:
        try:
            answer = input(prompt).strip().lower() or default
        except (EOFError, KeyboardInterrupt):
            raise UserExit()
        if answer in choices:
            return answer
        print("请输入括号中对应的字母。")


def atomic_json(path, value):
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def js_string(value):
    # 仅解析字符串，不执行网页中的 JavaScript。
    value = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m[1], 16)), value)
    value = re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m[1], 16)), value)
    return value.replace(r"\/", "/").replace(r"\'", "'").replace(r'\"', '"').replace(r"\\", "\\")


def parse_reader(source, reader_url):
    match = re.search(r"\b(?:var|let|const)\s+pages\s*=\s*(\[\s*(?:\[\s*\d+\s*,\s*\d+\s*\]\s*,?\s*)+\])", source)
    path_match = re.search(r'''\bjpgPath\s*:\s*(["'])(.*?)\1''', source, re.S)
    if not match or not path_match:
        raise DownloadError("未识别到图像阅读器的页码清单和图片路径。请粘贴已能显示书页的图像阅读网址；图书详情页、PDF 版阅读器和登录页不适用。")
    ranges = json.loads(re.sub(r",\s*]", "]", match[1]))
    if len(ranges) != len(PARTS):
        raise DownloadError("该阅读器的页面类型与已支持的版本不同，已停止，避免遗漏或乱序。")
    base = urllib.parse.urljoin(reader_url, html.unescape(js_string(path_match[2])))
    if urllib.parse.urlsplit(base).scheme not in ("http", "https"):
        raise DownloadError("图片路径不是 HTTP/HTTPS 地址。")
    pages = []
    sections = []
    for (start, end), (prefix, name) in zip(ranges, PARTS):
        if start < 1 or end > 100000:
            raise DownloadError("页面范围异常。")
        sections.append({"name": name, "start": start, "end": end, "count": max(0, end - start + 1)})
        for number in range(start, end + 1):
            key = prefix + str(number).zfill(6 - len(prefix))
            pages.append({"key": key, "section": name, "number": number,
                          "url": base + key + "?zoom=0"})
    if not pages or len(pages) > 100000:
        raise DownloadError("页面清单为空或异常。")
    if len({p['key'] for p in pages}) != len(pages):
        raise DownloadError("页面清单中出现重复编号，已停止。")
    title_match = re.search(r"<title[^>]*>(.*?)</title>", source, re.I | re.S)
    title = html.unescape(re.sub(r"<[^>]*>", "", title_match[1])).strip() if title_match else "电子书"
    # 认证路径会刷新；使用书籍目录和页面编号生成稳定标识，允许断点续传。
    identity_path = urllib.parse.urlsplit(base).path
    identity_path = identity_path.split("/libs/", 1)[-1]
    identity = hashlib.sha256((identity_path + json.dumps([p['key'] for p in pages])).encode()).hexdigest()
    return {"title": title, "identity": identity, "reader_url": reader_url,
            "sections": sections, "total": len(pages), "pages": pages}


class Client:
    def __init__(self, referer, cookie_file=None, delay=DEFAULT_DELAY, retries=3):
        jar = http.cookiejar.MozillaCookieJar()
        if cookie_file:
            jar.load(str(cookie_file), ignore_discard=True, ignore_expires=False)
        self.jar = jar
        self.local = threading.local()
        self.rate_lock = threading.Lock()
        self.stop = threading.Event()
        self.referer = referer
        self.delay = delay
        self.retries = retries
        self.last_request = 0.0

    def get(self, url):
        # 每个线程独立使用 opener；CookieJar 自带锁，可共享登录会话。
        if not hasattr(self.local, 'opener'):
            self.local.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        last_error = None
        for attempt in range(self.retries):
            # 对所有线程统一控制请求起始间隔，避免并发数倍增请求频率。
            with self.rate_lock:
                pause = max(0, self.delay - (time.monotonic() - self.last_request))
                if self.stop.wait(pause):
                    raise DownloadError("下载已停止。")
                self.last_request = time.monotonic()
            request = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": self.referer})
            try:
                with self.local.opener.open(request, timeout=45) as response:
                    data = response.read(64 * 1024 * 1024 + 1)
                    if len(data) > 64 * 1024 * 1024:
                        raise DownloadError("响应超过 64 MB，已停止读取。")
                    return data, response.headers.get_content_type(), response.geturl()
            except urllib.error.HTTPError as exc:
                last_error = exc
                exc.close()
                if exc.code in (401, 403, 429):
                    self.stop.set()
                    raise AccessError(f"服务器返回 HTTP {exc.code}。请在浏览器检查登录、访问权限或访问频率，稍后重新运行；已下载页面会保留。") from exc
                if exc.code < 500:
                    raise DownloadError(f"服务器返回 HTTP {exc.code}。") from exc
            except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
                last_error = exc
            if attempt + 1 < self.retries:
                if self.stop.wait(2 ** (attempt + 1)):
                    raise DownloadError("下载已停止。")
        raise DownloadError(f"网络请求失败，已尝试 {self.retries} 次：{last_error}") from last_error


def check_image(data):
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.verify()
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            width, height = image.size
            if width < 32 or height < 32:
                raise DownloadError("图片尺寸过小，可能是占位图片。")
            return width, height
    except DownloadError:
        raise
    except Exception as exc:
        raise DownloadError("响应不是有效图片，可能是登录页、验证码或已失效的链接。请在浏览器恢复正常阅读后重新运行。") from exc


def safe_name(title):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", title).strip(" .")[:100]
    if not name or name.upper().split('.')[0] in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        name = "电子书_" + name
    return name


def merge_pdf(paths, target, title):
    temporary = target.with_suffix(".partial.pdf")
    try:
        with temporary.open("wb") as stream:
            img2pdf.convert([str(p) for p in paths], outputstream=stream, title=title)
        with temporary.open("rb") as stream:
            reader = PdfReader(stream)
            if len(reader.pages) != len(paths):
                raise DownloadError("合并后的 PDF 页数不一致，未发布最终文件。")
            for page in reader.pages:
                if float(page.mediabox.width) <= 0 or float(page.mediabox.height) <= 0:
                    raise DownloadError("合并后的 PDF 页面尺寸异常。")
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()


DEFAULT_OUTPUT = Path(__file__).resolve().parent / "downloads"


def recommended_settings(total):
    # 经验起点：长书减少持续请求压力，不把这些值当作网站允许的频率。
    return (2, 0.5) if total > 600 else (DEFAULT_WORKERS, DEFAULT_DELAY)


def estimate_seconds(total, pending, workers, delay):
    # 尚未测速，假设每页完成一次请求需 1—3 秒；同时考虑并发与全局节流。
    def duration(latency, overhead_per_page):
        network = max((pending - 1) * delay + latency,
                      math.ceil(pending / workers) * latency) if pending else 0
        return network + 2 + total * overhead_per_page
    return duration(1, 0.01), duration(3, 0.08)


def format_duration(seconds):
    seconds = max(1, math.ceil(seconds))
    if seconds < 60:
        return f"{seconds} 秒"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} 分 {seconds} 秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 小时 {minutes} 分"


def ask_number(prompt, convert, valid, explanation, default):
    while True:
        entered = input(prompt).strip()
        if not entered:
            return default
        try:
            value = convert(entered)
            if valid(value):
                return value
        except (ValueError, OverflowError):
            pass
        print(explanation)


def configure_download(args, selected, images):
    total = len(selected)
    existing = 0
    for index, page in enumerate(selected, 1):
        path = images / f"{index:06d}_{page['key']}.img"
        try:
            existing += path.stat().st_size > 0
        except OSError:
            pass
    pending = total - existing
    recommended_workers, recommended_delay = recommended_settings(total)
    workers = args.workers if args.workers is not None else recommended_workers
    delay = args.delay if args.delay is not None else recommended_delay
    print(f"\n本次处理 {total} 页；已有 {existing} 张图片待校验，预计还需请求 {pending} 页。")
    print(f"兼顾速度与稳定性的建议：{recommended_workers} 个并发，所有线程统一间隔 {recommended_delay:g} 秒发起请求。")
    print("网络良好、连续下载正常后可尝试 4 并发 / 0.2 秒；网络不稳可用 2 并发 / 0.6 秒。")
    print("并发数控制同时进行的下载数量；间隔控制所有线程合计的发起频率，间隔越小越容易触发限流。")
    low, high = estimate_seconds(total, pending, recommended_workers, recommended_delay)
    print(f"推荐方案预计耗时：{format_duration(low)} — {format_duration(high)}（含校验和合并）。")
    print("此为经验估算，按每页请求耗时 1—3 秒计算；不含失败重试和等待输入，实际受网络、图片大小、服务器及磁盘速度影响。")
    if getattr(args, 'interactive', False) and not getattr(args, '_settings_confirmed', False):
        workers = ask_number(f"并发下载数量 [1—16，推荐 {recommended_workers}，回车采用 {workers}]：", int,
                             lambda n: 1 <= n <= 16, "请输入 1—16 之间的整数。", workers)
        delay = ask_number(f"请求间隔（秒）[推荐 {recommended_delay:g}，回车采用 {delay:g}]：", float,
                           lambda n: math.isfinite(n) and n >= 0, "请输入不小于 0 的有限数值，例如 0.35。", delay)
        args._settings_confirmed = True
    args.workers, args.delay = workers, delay
    low, high = estimate_seconds(total, pending, workers, delay)
    print(f"采用 {workers} 个并发 / {delay:g} 秒间隔；预计耗时：{format_duration(low)} — {format_duration(high)}。", flush=True)
    if delay < 0.2 or workers > 4:
        print("当前设置比推荐方案更激进；若出现限流，请改用更少并发和更长间隔。", flush=True)


def choose_output(output, interactive):
    if output is None:
        entered = input(f"保存到哪里？直接回车使用默认目录\n[{DEFAULT_OUTPUT}]：\n") if interactive else ""
        output = entered.strip().strip('\"\'') or DEFAULT_OUTPUT
    return Path(os.path.expandvars(str(output))).expanduser().resolve()


def fetch_page(client, index, page, images, cached_details):
    path = images / f"{index:06d}_{page['key']}.img"
    cached = False
    if path.exists():
        try:
            content = path.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            prior = cached_details.get(page['key'], {})
            # 哈希与此前已验证图片一致时，无需再次完整解码，提高续传速度。
            if prior.get('sha256') == digest and len(prior.get('size', [])) == 2:
                dimensions = prior['size']
            else:
                dimensions = check_image(content)
            cached = True
        except (DownloadError, OSError):
            pass
    if not cached:
        content, mime, _ = client.get(page['url'])
        if mime in ("text/html", "application/json", "text/plain"):
            client.stop.set()
            raise AccessError("服务器返回提示页面，可能需要登录或验证码。已停止，请在浏览器恢复正常阅读后重新运行。")
        dimensions = check_image(content)
        digest = hashlib.sha256(content).hexdigest()
        part = path.with_suffix(".part")
        part.write_bytes(content)
        os.replace(part, path)
    detail = {"index": index, "key": page['key'], "size": list(dimensions), "sha256": digest}
    return path, detail, cached


def download_pages(client, selected, images, folder, workers, interactive=False):
    cached_details = {}
    for name in ('report.json', 'progress.json'):
        try:
            old_report = json.loads((folder / name).read_text(encoding='utf-8'))
            cached_details.update({p['key']: p for p in old_report.get('images', [])})
        except (OSError, ValueError, KeyError, TypeError):
            pass
    results, failures = {}, []
    tasks = iter(enumerate(selected, 1))
    pool = ThreadPoolExecutor(max_workers=workers)
    pending = {}

    def submit_next():
        if client.stop.is_set():
            return
        item = next(tasks, None)
        if item:
            index, page = item
            future = pool.submit(fetch_page, client, index, page, images, cached_details)
            pending[future] = (index, page)

    try:
        # 仅维持 workers 个任务，停止时不再排入后续页面。
        for _ in range(workers):
            submit_next()
        finished = 0
        while pending:
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            # 优先发现访问限制，防止其他完成任务继续提交新请求。
            for future in done:
                error = future.exception()
                if isinstance(error, AccessError):
                    raise error
            for future in done:
                index, page = pending.pop(future)
                finished += 1
                try:
                    path, detail, cached = future.result()
                    results[index] = (path, detail)
                    print(f"[{finished}/{len(selected)}] {page['section']} {page['number']}  {'已缓存' if cached else '已保存'}", flush=True)
                except OSError:
                    # 写入失败通常影响整次下载，不能当作某一页不存在来跳过。
                    raise
                except DownloadError as exc:
                    print(f"\n第 {index} 个文件（{page['section']}第 {page['number']} 页）下载失败。", flush=True)
                    explain_error(exc, "单页下载")
                    action = ask_choice("[r] 重试此页 / [s] 跳过此页 / [b] 重新开始 / [q] 结束（默认 r）：", {'r', 's', 'b', 'q'}, 'r') if interactive else None
                    if action == 'r':
                        finished -= 1
                        retry = pool.submit(fetch_page, client, index, page, images, cached_details)
                        pending[retry] = (index, page)
                        continue
                    if action == 'b':
                        raise RestartRequested()
                    if action == 'q':
                        raise UserExit()
                    failures.append({"index": index, "key": page['key'], "section": page['section'],
                                     "number": page['number'], "error": str(exc), "skipped": action == 's'})
                    if action == 's':
                        print("已跳过；最终 PDF 会标注“缺页”，下次重新开始仍会尝试补下载此页。", flush=True)
                submit_next()
    finally:
        client.stop.set()
        for future in pending:
            future.cancel()
        pool.shutdown(wait=True, cancel_futures=True)
        # 收集已经在途并成功落盘的任务，让恢复过程也能快速校验缓存。
        for future, (index, page) in pending.items():
            if future.cancelled():
                continue
            try:
                path, detail, _ = future.result()
                results[index] = (path, detail)
            except Exception:
                pass
        cached_details.update({detail['key']: detail for _, detail in results.values()})
        try:
            atomic_json(folder / 'progress.json', {'images': list(cached_details.values())})
        except OSError as exc:
            print(f"进度记录未能写入：{exc}。已经保存的图片仍保留。", flush=True)
    # 下载可乱序完成，PDF 和报告始终按原始页码排序。
    ordered = [results[index] for index in sorted(results)]
    return [item[0] for item in ordered], [item[1] for item in ordered], sorted(failures, key=lambda p: p['index'])


def run(args):
    url = (args.url or input("请在浏览器中打开并粘贴超星汇雅【图像阅读】网址，然后按回车：\n")).strip().strip('"').replace("\\&", "&")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise DownloadError("请输入完整的 http:// 或 https:// 网址。")
    output = choose_output(args.output, interactive=not args.url)
    # 保存本次输入，恢复时直接复用；不再次询问已选的目录。
    args.url, args.output = url, output
    args._stage = "读取阅读页面"
    client = Client(url, args.cookies, args.delay if args.delay is not None else DEFAULT_DELAY, args.retries)
    print("正在读取页面清单……", flush=True)
    data, _, final_url = client.get(url)
    # 阅读器的 HTML 通常为 UTF-8，兼容旧版 GBK 页面。
    try:
        source = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        source = data.decode("gb18030", errors="replace")
    plan = parse_reader(source, final_url)
    client.referer = final_url
    title = safe_name(plan['title'])
    if getattr(args, '_book_identity', plan['identity']) != plan['identity']:
        raise DownloadError("新网址对应另一份页面清单，请重新启动脚本处理另一本书，原书的图片仍保留。")
    folder = getattr(args, '_book_folder', None) or output / (title + "_" + plan['identity'][:8])
    args._book_identity, args._book_folder = plan['identity'], folder
    folder.mkdir(parents=True, exist_ok=True)
    images = folder / "images"
    images.mkdir(exist_ok=True)
    atomic_json(folder / "pages.json", plan)
    print(f"书名：{plan['title']}\n识别到 {plan['total']} 页。")
    print("，".join(f"{p['name']} {p['count']} 页" for p in plan['sections'] if p['count']))
    print(f"输出目录：{folder.resolve()}", flush=True)
    if args.expected_pages is not None and args.expected_pages != plan['total']:
        raise DownloadError(f"网页声明 {plan['total']} 页，与要求的 {args.expected_pages} 页不同。已保存清单，请核对后再下载。")
    if args.inspect:
        print("仅检查清单，未下载图片。")
        return
    selected = plan['pages'][:args.sample] if args.sample else plan['pages']
    args._stage = "设置下载参数"
    configure_download(args, selected, images)
    client.delay = args.delay
    workers = args.workers
    print(f"下载并发数：{workers}；全局请求间隔：{args.delay:g} 秒", flush=True)
    args._stage = "下载图片"
    paths, details, failures = download_pages(client, selected, images, folder, workers, getattr(args, 'interactive', False))
    hashes = {}
    duplicates = []
    for detail in details:
        digest, index = detail['sha256'], detail['index']
        if digest in hashes:
            duplicates.append([hashes[digest], index])
        else:
            hashes[digest] = index
    report = {"declared_pages": plan['total'], "requested_pages": len(selected),
              "saved_pages": len(paths), "failures": failures, "duplicate_pairs": duplicates, "images": details,
              "scope": "网页声明的页面；未声明的封面等无法保证包含", "pdf_verified": False,
              "complete": False, "skipped_pages": [p for p in failures if p.get('skipped')]}
    atomic_json(folder / "report.json", report)
    if any(not p.get('skipped') for p in failures):
        raise DownloadError(f"有 {len(failures)} 页未成功，未生成可能缺页的 PDF。重新运行相同网址可续传；详情见 report.json。")
    if not paths:
        raise DownloadError("没有成功下载的图片，无法合并 PDF。请检查网址和网络后重新开始。")
    target = folder / (title + ("_测试" if args.sample else "") + ("_缺页" if failures else "") + ".pdf")
    args._stage = "合并 PDF"
    print("正在合并 PDF……", flush=True)
    merge_pdf(paths, target, plan['title'])
    report.update(pdf_verified=True, pdf=str(target.resolve()), pdf_pages=len(paths), complete=len(paths) == plan['total'])
    atomic_json(folder / "report.json", report)
    print(f"\n完成：{target.resolve()}\n已核验 PDF 页数：{len(paths)}")
    if args.sample:
        print("这是抽样测试 PDF，不是整本。")
    if failures:
        print(f"注意：你选择跳过了 {len(failures)} 页，当前 PDF 缺页；具体页码见 report.json。")
    if duplicates:
        print(f"发现 {len(duplicates)} 组内容相同的页面，已保留原顺序；请查看报告，可能是空白页或重复响应。")


def run_with_recovery(args):
    while True:
        try:
            run(args)
            return 0
        except UserExit:
            print("\n已结束；已经保存的图片保留。")
            return 1
        except EOFError:
            print("\n未收到终端输入，已结束；已经保存的图片保留。")
            return 1
        except (Exception, KeyboardInterrupt) as exc:
            if not isinstance(exc, RestartRequested):
                explain_error(exc, getattr(args, '_stage', '运行'))
            print("已下载完毕的图片保留。重新开始会重新读取清单，并跳过有效缓存。", flush=True)
            if not getattr(args, 'interactive', False):
                return 1
            try:
                action = ask_choice("是否重新开始？[y] 是 / [n] 否（默认 n）：", {'y', 'n'}, 'n')
                if action == 'n':
                    return 1
                new_url = input("可粘贴同一本书的新图像阅读网址；直接回车沿用上次网址：\n").strip().strip('"')
                if new_url:
                    args.url = new_url
                if isinstance(exc, OSError):
                    entered = input("保存目录：回车沿用原目录，或输入新目录：\n").strip().strip('\"\'')
                    if entered:
                        old_folder = getattr(args, '_book_folder', None)
                        args.output = choose_output(Path(entered), False)
                        args._book_folder = None
                        if old_folder:
                            print(f"原目录的图片保留在 {old_folder}；新目录会单独下载。")
                if isinstance(exc, AccessError):
                    args.workers = min(args.workers or DEFAULT_WORKERS, 2)
                    args.delay = max(args.delay if args.delay is not None else DEFAULT_DELAY, 1.0)
                    print("恢复下载时自动降低并发至最多 2，并增加请求间隔至至少 1 秒。")
            except (EOFError, KeyboardInterrupt, UserExit):
                print("\n已结束；已经保存的图片保留。")
                return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", nargs="?", help="不填则运行后粘贴阅读网址")
    parser.add_argument("-o", "--output", type=Path, help="输出根目录；交互运行时询问，回车默认 downloads")
    parser.add_argument("--cookies", type=Path, help="可选：本人会话导出的 Netscape 格式 cookies 文件")
    parser.add_argument("--workers", type=int, help="并发下载数，可选 1—16；默认推荐 3，超过 600 页推荐 2")
    parser.add_argument("--delay", type=float, help="所有线程的请求起始间隔秒数；默认推荐 0.35，超过 600 页推荐 0.5")
    parser.add_argument("--retries", type=int, default=3, help="网络或服务器临时错误重试次数，默认 3")
    parser.add_argument("--expected-pages", type=int, help="可选：预期总页数；不符时停止")
    parser.add_argument("--inspect", action="store_true", help="仅识别清单，不下载")
    parser.add_argument("--sample", type=int, metavar="N", help="仅下载前 N 页生成测试 PDF")
    parser.add_argument("--non-interactive", action="store_true", help="不询问跳过或重新开始，适合自动化运行")
    args = parser.parse_args()
    if ((args.workers is not None and not 1 <= args.workers <= 16)
            or (args.delay is not None and (not math.isfinite(args.delay) or args.delay < 0))
            or args.retries < 1 or (args.sample is not None and args.sample < 1)):
        parser.error("workers 为 1—16，delay 不得为负，retries 和 sample 必须为正数。")
    args.interactive = not args.non_interactive
    return run_with_recovery(args)


if __name__ == "__main__":
    raise SystemExit(main())
