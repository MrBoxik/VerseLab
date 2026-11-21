import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

SPEC = importlib.util.spec_from_file_location('lyrics_app', Path(__file__).resolve().parents[1] / 'app.py')
app = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(app)


class RangeTests(unittest.TestCase):
    def test_normal_open_and_suffix_ranges(self):
        self.assertEqual(app.byte_range('bytes=0-4', 10), (0, 4))
        self.assertEqual(app.byte_range('bytes=7-', 10), (7, 9))
        self.assertEqual(app.byte_range('bytes=-3', 10), (7, 9))
        self.assertEqual(app.byte_range('bytes=2-100', 10), (2, 9))
        self.assertIsNone(app.byte_range(None, 10))

    def test_invalid_ranges(self):
        for value in ['bytes=10-', 'bytes=8-2', 'bytes=-0', 'bytes=0-1,4-5', 'garbage', 'bytes=-']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                app.byte_range(value, 10)


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.original_data = app.DATA
        app.DATA = Path(cls.temp.name)
        (app.DATA / 'tracks').mkdir()
        cls.audio = app.DATA / 'fixture.mp3'
        cls.audio.write_bytes(bytes(range(256)) * 4)
        app.LIBRARY['a' * 24] = cls.audio
        cls.server = app.ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        app.DATA = cls.original_data
        app.LIBRARY.pop('a' * 24, None)
        cls.temp.cleanup()

    def request(self, path, data=None, headers=None):
        if data is not None:
            data = json.dumps(data).encode()
        return urllib.request.urlopen(urllib.request.Request(self.url + path, data=data, headers=headers or {}), timeout=5)

    def test_audio_seek_returns_exact_requested_bytes_and_never_changes_file(self):
        before = self.audio.read_bytes()
        with self.request('/api/audio?id=' + 'a' * 24, headers={'Range': 'bytes=10-19'}) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.headers['Content-Range'], 'bytes 10-19/1024')
            self.assertEqual(response.read(), before[10:20])
        self.assertEqual(self.audio.read_bytes(), before)

    def test_invalid_audio_range_is_416(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('/api/audio?id=' + 'a' * 24, headers={'Range': 'bytes=9999-'})
        self.assertEqual(error.exception.code, 416)
        error.exception.close()

    def test_per_song_adjustments_round_trip(self):
        payload = {'id': 'b' * 24, 'state': {'offset': 5.25, 'scale': 1.02, 'lines': [{'time': 1, 'text': 'Example'}]}}
        with self.request('/api/state', payload) as response:
            self.assertTrue(json.load(response)['saved'])
        with self.request('/api/state?id=' + 'b' * 24) as response:
            self.assertEqual(json.load(response), payload['state'])

    def test_foreign_origin_cannot_write_settings(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('/api/state', {'id': 'b' * 24, 'state': {}}, {'Origin': 'https://example.com'})
        self.assertEqual(error.exception.code, 403)
        error.exception.close()

    def test_cannot_use_state_or_static_path_to_read_arbitrary_files(self):
        for route in ['/api/state?id=../../config', '/config.json', '/app.py', '/data/tracks/example.json']:
            with self.subTest(route=route), self.assertRaises(urllib.error.HTTPError) as error:
                self.request(route)
            self.assertIn(error.exception.code, [400, 404])
            error.exception.close()


if __name__ == '__main__':
    unittest.main()
