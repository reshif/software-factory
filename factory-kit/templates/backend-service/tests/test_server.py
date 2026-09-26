import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from app.server import make_handler
from app.store import Store


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.store = Store()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.store))
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)

    def _url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def test_healthz(self) -> None:
        with urllib.request.urlopen(self._url("/healthz")) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(json.loads(resp.read()), {"status": "ok"})

    def test_create_and_fetch_item(self) -> None:
        body = json.dumps({"name": "widget"}).encode()
        req = urllib.request.Request(
            self._url("/items"), data=body, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 201)
            created = json.loads(resp.read())
        with urllib.request.urlopen(self._url(f"/items/{created['id']}")) as resp:
            self.assertEqual(json.loads(resp.read()), created)

    def test_missing_item_is_404(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(self._url("/items/999999"))
        self.assertEqual(ctx.exception.code, 404)

    def test_delete_item(self) -> None:
        body = json.dumps({"name": "temp"}).encode()
        req = urllib.request.Request(self._url("/items"), data=body, method="POST")
        with urllib.request.urlopen(req) as resp:
            created = json.loads(resp.read())
        req = urllib.request.Request(self._url(f"/items/{created['id']}"), method="DELETE")
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 204)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(self._url(f"/items/{created['id']}"))
        self.assertEqual(ctx.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
