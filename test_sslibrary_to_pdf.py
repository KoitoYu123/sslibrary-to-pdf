"""仅使用本地模拟阅读器测试，不请求任何真实图书网站。"""
import argparse
from contextlib import redirect_stdout, redirect_stderr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
from pathlib import Path
import tempfile
import threading
import time
import unittest
import subprocess
import sys
from unittest.mock import patch

from PIL import Image
from pypdf import PdfReader
import sslibrary_to_pdf as app


HTML = '''<html><title>通用测试图书</title><script>
var pages = [[1,1],[1,1],[1,1],[1,2],[1,1],[1,3],[1,1],[2,2]];
var opts = {pages:pages, jpgPath:"/libs/test-book/"};
</script></html>'''


class LocalReader(BaseHTTPRequestHandler):
    hits = []
    fail_key = None
    fail_code = 404
    latency = 0
    active = 0
    peak = 0
    lock = threading.Lock()

    def log_message(self, *args):
        pass

    def do_GET(self):
        type(self).hits.append(self.path)
        if self.path == '/reader':
            content, mime = HTML.encode('utf8'), 'text/html'
        elif '/libs/test-book/' in self.path:
            key = self.path.split('/')[-1].split('?')[0]
            if key == type(self).fail_key:
                self.send_error(type(self).fail_code)
                return
            with type(self).lock:
                type(self).active += 1
                type(self).peak = max(type(self).peak, type(self).active)
            time.sleep(type(self).latency)
            output = io.BytesIO()
            color = tuple(app.hashlib.sha256(key.encode()).digest()[:3])
            Image.new('RGB', (120, 180), color).save(output, format='JPEG')
            content, mime = output.getvalue(), 'image/jpeg'
            with type(self).lock:
                type(self).active -= 1
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(content)))
        self.end_headers()
        self.wfile.write(content)


class Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), LocalReader)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f'http://127.0.0.1:{cls.server.server_port}/reader'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        LocalReader.hits = []
        LocalReader.fail_key = None
        LocalReader.fail_code = 404
        LocalReader.latency = 0
        LocalReader.active = 0
        LocalReader.peak = 0

    def args(self, root, **kwargs):
        values = dict(url=self.url, cookies=None, delay=0, retries=1,
                      workers=4, output=Path(root), expected_pages=None, inspect=False, sample=None,
                      _settings_confirmed=True)
        values.update(kwargs)
        return argparse.Namespace(**values)

    def test_page_order_and_rejection(self):
        plan = app.parse_reader(HTML, self.url)
        self.assertEqual(plan['total'], 11)
        self.assertEqual([p['key'] for p in plan['pages']],
                         ['cov001','bok001','leg001','fow001','fow002','!00001',
                          '000001','000002','000003','att001','cov002'])
        with self.assertRaises(app.DownloadError):
            app.parse_reader('<html>登录页</html>', self.url)
        with self.assertRaises(app.DownloadError):
            app.check_image(b'<html>not an image</html>')
        tiny = io.BytesIO()
        Image.new('RGB', (1, 1)).save(tiny, 'PNG')
        with self.assertRaises(app.DownloadError):
            app.check_image(tiny.getvalue())

    def test_download_merge_resume_and_repair(self):
        with tempfile.TemporaryDirectory() as root, redirect_stdout(io.StringIO()):
            args = self.args(root)
            app.run(args)
            folder = next(Path(root).iterdir())
            target = next(folder.glob('*.pdf'))
            with target.open('rb') as stream:
                self.assertEqual(len(PdfReader(stream).pages), 11)
            LocalReader.hits = []
            app.run(args)
            self.assertEqual(LocalReader.hits, ['/reader'])
            next((folder / 'images').iterdir()).write_bytes(b'corrupted')
            LocalReader.hits = []
            app.run(args)
            self.assertEqual(len(LocalReader.hits), 2)
            report = app.json.loads((folder / 'report.json').read_text('utf8'))
            self.assertTrue(report['pdf_verified'])
            self.assertFalse(report['failures'])

    def test_missing_page_does_not_produce_pdf(self):
        with tempfile.TemporaryDirectory() as root, redirect_stdout(io.StringIO()):
            LocalReader.fail_key = '000002'
            with self.assertRaises(app.DownloadError):
                app.run(self.args(root))
            folder = next(Path(root).iterdir())
            self.assertFalse(list(folder.glob('*.pdf')))
            LocalReader.fail_key = None
            LocalReader.hits = []
            app.run(self.args(root))
            self.assertEqual(len(LocalReader.hits), 2)
            self.assertEqual(len(list(folder.glob('*.pdf'))), 1)

    def test_inspect_and_expected_count(self):
        with tempfile.TemporaryDirectory() as root, redirect_stdout(io.StringIO()):
            app.run(self.args(root, inspect=True))
            self.assertEqual(LocalReader.hits, ['/reader'])
            with self.assertRaises(app.DownloadError):
                app.run(self.args(root, expected_pages=99))
            self.assertFalse(list(Path(root).rglob('*.pdf')))

    def test_sample_pdf_is_named_as_sample(self):
        with tempfile.TemporaryDirectory() as root, redirect_stdout(io.StringIO()):
            app.run(self.args(root, sample=2))
            target = next(Path(root).rglob('*.pdf'))
            self.assertTrue(target.stem.endswith('_测试'))
            with target.open('rb') as stream:
                self.assertEqual(len(PdfReader(stream).pages), 2)

    def test_interactive_cli(self):
        with tempfile.TemporaryDirectory() as root:
            result = subprocess.run(
                [sys.executable, '-X', 'utf8', str(Path(app.__file__)),
                 '-o', root, '--sample', '2', '--delay', '0.5'],
                input=self.url + '\n\n\n', encoding='utf8', capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('已核验 PDF 页数：2', result.stdout)
            self.assertEqual(len(list(Path(root).rglob('*.pdf'))), 1)

    def test_interactive_output_and_default(self):
        with tempfile.TemporaryDirectory() as root, redirect_stdout(io.StringIO()):
            custom = Path(root) / '自定义目录'
            with patch('builtins.input', return_value=f'"{custom}"'):
                self.assertEqual(app.choose_output(None, True), custom.resolve())
            with patch('builtins.input', return_value=''):
                self.assertEqual(app.choose_output(None, True), app.DEFAULT_OUTPUT)
            with patch('builtins.input', side_effect=AssertionError('不可提示')):
                self.assertEqual(app.choose_output(None, False), app.DEFAULT_OUTPUT)
            with patch('builtins.input', side_effect=[self.url, str(custom)]):
                app.run(self.args(root, url=None, output=None, sample=2))
            self.assertEqual(len(list(custom.rglob('*.pdf'))), 1)

    def test_concurrency_preserves_page_order(self):
        LocalReader.latency = 0.06
        with tempfile.TemporaryDirectory() as root, redirect_stdout(io.StringIO()):
            app.run(self.args(root, workers=4))
            self.assertGreater(LocalReader.peak, 1)
            self.assertLessEqual(LocalReader.peak, 4)
            folder = next(Path(root).iterdir())
            images = sorted((folder / 'images').glob('*.img'))
            with next(folder.glob('*.pdf')).open('rb') as stream:
                reader = PdfReader(stream)
                for page, image_path in zip(reader.pages, images):
                    with Image.open(image_path) as original:
                        self.assertEqual(page.images[0].image.getpixel((0, 0)), original.getpixel((0, 0)))

    def test_access_limit_stops_queue(self):
        LocalReader.fail_key = 'cov001'
        LocalReader.fail_code = 429
        LocalReader.latency = 0.05
        with tempfile.TemporaryDirectory() as root, redirect_stdout(io.StringIO()):
            with self.assertRaises(app.AccessError):
                app.run(self.args(root, workers=2))
            self.assertLessEqual(len(LocalReader.hits), 3)  # 清单 + 最多两个在途请求
            self.assertFalse(list(Path(root).rglob('*.pdf')))

    def test_skip_failed_page_marks_partial_pdf(self):
        LocalReader.fail_key = '000002'
        with tempfile.TemporaryDirectory() as root, redirect_stdout(io.StringIO()), patch('builtins.input', return_value='s') as prompt:
            app.run(self.args(root, interactive=True))
            prompt.assert_called_once()
            target = next(Path(root).rglob('*.pdf'))
            self.assertTrue(target.stem.endswith('_缺页'))
            with target.open('rb') as stream:
                self.assertEqual(len(PdfReader(stream).pages), 10)
            report = app.json.loads((target.parent / 'report.json').read_text('utf8'))
            self.assertFalse(report['complete'])
            self.assertEqual(report['skipped_pages'][0]['key'], '000002')
            # 下次恢复时跳过记录不妨碍补齐缺页。
            LocalReader.fail_key = None
            LocalReader.hits = []
            app.run(self.args(root))
            self.assertEqual(len(LocalReader.hits), 2)
            self.assertEqual(len(list(target.parent.glob('*.pdf'))), 2)

    def test_retry_failed_page(self):
        LocalReader.fail_key = '000002'
        def retry(_):
            LocalReader.fail_key = None
            return 'r'
        with tempfile.TemporaryDirectory() as root, redirect_stdout(io.StringIO()), patch('builtins.input', side_effect=retry):
            app.run(self.args(root, interactive=True))
            self.assertEqual(sum('000002?' in hit for hit in LocalReader.hits), 2)
            with next(Path(root).rglob('*.pdf')).open('rb') as stream:
                self.assertEqual(len(PdfReader(stream).pages), 11)

    def test_restart_keeps_successful_images(self):
        LocalReader.fail_key = '000002'
        replies = iter(['b', 'y', ''])
        def answer(prompt):
            value = next(replies)
            if '新图像阅读网址' in prompt:
                LocalReader.fail_key = None
            return value
        with tempfile.TemporaryDirectory() as root, redirect_stdout(io.StringIO()), patch('builtins.input', side_effect=answer):
            self.assertEqual(app.run_with_recovery(self.args(root, workers=1, interactive=True)), 0)
            self.assertEqual(LocalReader.hits.count('/reader'), 2)
            self.assertEqual(sum('/000001?' in hit for hit in LocalReader.hits), 1)
            with next(Path(root).rglob('*.pdf')).open('rb') as stream:
                self.assertEqual(len(PdfReader(stream).pages), 11)

    def test_access_error_restart_preserves_cache(self):
        LocalReader.fail_key = '000002'
        LocalReader.fail_code = 403
        replies = iter(['y', ''])
        def answer(prompt):
            value = next(replies)
            if '新图像阅读网址' in prompt:
                LocalReader.fail_key = None
            return value
        args = None
        with tempfile.TemporaryDirectory() as root, redirect_stdout(io.StringIO()), patch('builtins.input', side_effect=answer):
            args = self.args(root, workers=1, interactive=True)
            self.assertEqual(app.run_with_recovery(args), 0)
            self.assertEqual(sum('/000001?' in hit for hit in LocalReader.hits), 1)
            self.assertGreaterEqual(args.delay, 1)

    def test_quit_and_eof_keep_images(self):
        LocalReader.fail_key = '000002'
        for response in ['q', EOFError()]:
            with self.subTest(response=response), tempfile.TemporaryDirectory() as root, redirect_stdout(io.StringIO()):
                effect = response if isinstance(response, Exception) else lambda _: response
                with patch('builtins.input', side_effect=effect):
                    self.assertEqual(app.run_with_recovery(self.args(root, workers=1, interactive=True)), 1)
                self.assertGreater(len(list(Path(root).rglob('*.img'))), 0)
                self.assertFalse(list(Path(root).rglob('*.pdf')))

    def test_diagnostics_and_unexpected_error_recovery(self):
        output = io.StringIO()
        with redirect_stdout(output):
            app.explain_error(OSError(app.errno.ENOSPC, 'full'))
            app.explain_error(PermissionError('locked'))
            app.explain_error(app.AccessError('HTTP 429'))
            app.explain_error(KeyboardInterrupt())
        for text in ['磁盘空间不足', '占用', '请求频率', 'Ctrl+C', '解决办法']:
            self.assertIn(text, output.getvalue())
        args = self.args('.', interactive=True)
        with patch.object(app, 'run', side_effect=[RuntimeError('unexpected'), None]), patch('builtins.input', side_effect=['y', '']), redirect_stdout(io.StringIO()):
            self.assertEqual(app.run_with_recovery(args), 0)

    def test_settings_prompt_before_image_download(self):
        replies = iter(['2', '0.1'])
        def answer(_):
            self.assertEqual(LocalReader.hits, ['/reader'])
            return next(replies)
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as root, redirect_stdout(output), patch('builtins.input', side_effect=answer):
            args = self.args(root, workers=None, delay=None, interactive=True, sample=2, _settings_confirmed=False)
            app.run(args)
            self.assertEqual((args.workers, args.delay), (2, 0.1))
            self.assertTrue(list(Path(root).rglob('*.pdf')))
        self.assertIn('推荐方案预计耗时', output.getvalue())
        self.assertIn('经验估算', output.getvalue())

    def test_settings_defaults_and_invalid_input(self):
        pages = [{'key': '000001'}]
        with tempfile.TemporaryDirectory() as root, redirect_stdout(io.StringIO()):
            args = self.args(root, workers=None, delay=None, interactive=True, _settings_confirmed=False)
            with patch('builtins.input', side_effect=['', '']):
                app.configure_download(args, pages, Path(root))
            self.assertEqual((args.workers, args.delay), (3, 0.35))
            args._settings_confirmed = False
            with patch('builtins.input', side_effect=['17', 'abc', '2', 'nan', '-1', 'inf', '0.4']):
                app.configure_download(args, pages, Path(root))
            self.assertEqual((args.workers, args.delay), (2, 0.4))
            with patch('builtins.input', side_effect=AssertionError('恢复时复用参数')):
                app.configure_download(args, pages, Path(root))

    def test_eta_and_long_book_recommendation(self):
        self.assertEqual(app.recommended_settings(601), (2, 0.5))
        low, high = app.estimate_seconds(400, 400, 3, 0.35)
        self.assertGreater(high, low)
        self.assertGreater(app.estimate_seconds(400, 400, 1, 1)[0], low)
        self.assertLess(app.estimate_seconds(400, 0, 3, 0.35)[1], high)
        self.assertIn('分', app.format_duration(120))
        with tempfile.TemporaryDirectory() as root:
            result = subprocess.run([sys.executable, '-X', 'utf8', str(Path(app.__file__)),
                                     self.url, '-o', root, '--sample', '1', '--non-interactive'],
                                    input='', encoding='utf8', capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('采用 3 个并发 / 0.35 秒间隔', result.stdout)


if __name__ == '__main__':
    unittest.main()
