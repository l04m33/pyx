import unittest
import asyncio
import pyx.http as http


def create_dummy_message():
    msg = http.HttpMessage(None)
    msg.headers = [
        http.HttpHeader('Server', 'Pyx'),
        http.HttpHeader('Cookie', 'a'),
        http.HttpHeader('Cookie', 'b'),
    ]
    return msg


def create_dummy_request():
    loop = asyncio.get_event_loop()
    reader = asyncio.StreamReader(loop=loop)
    conn = http.HttpConnection(reader, None)
    return http.HttpRequest(conn)


class TestHttpMessage(unittest.TestCase):
    def test_get_header(self):
        msg = create_dummy_message()

        self.assertEqual(msg.get_header("server"), ["Pyx"])
        self.assertEqual(msg.get_header("SERVER"), ["Pyx"])
        self.assertEqual(msg.get_header("pragma"), [])

        self.assertEqual(msg.get_header("cookie"), ["a", "b"])
        self.assertEqual(msg.get_first_header("cookie"), "a")
        self.assertTrue(msg.get_first_header("pragma") is None)

    def test_write_headers(self):
        msg = create_dummy_message()
        self.assertEqual(msg.write_headers(),
                         ['Server: Pyx', 'Cookie: a', 'Cookie: b'])

        msg.headers = []
        self.assertEqual(msg.write_headers(), [])


class TestHttpRequest(unittest.TestCase):
    def test_parse_req_line(self):
        req = create_dummy_request()

        req._parse_req_line(b'POST / HTTP/1.1\r\n')
        self.assertEqual(req.method, 'POST')
        self.assertEqual(req.path, '/')
        self.assertTrue(req.query is None)
        self.assertEqual(req.protocol, 'HTTP')
        self.assertEqual(req.version, (1, 1))

        req._parse_req_line(
            b'GET /some/path?some=query&some_other=query HTTP/1.1\r\n')
        self.assertEqual(req.method, 'GET')
        self.assertEqual(req.path, '/some/path')
        self.assertEqual(req.query, 'some=query&some_other=query')

        with self.assertRaises(http.BadHttpRequestError):
            req._parse_req_line(b'')

    def test_parse_header(self):
        req = create_dummy_request()

        req._parse_header(b'Server: Pyx\r\n')
        self.assertEqual(req.headers, [http.HttpHeader('Server', 'Pyx')])

        req.headers = []
        with self.assertRaises(http.BadHttpHeaderError):
            req._parse_header(b'Server\r\n')

        req.headers = []
        req._parse_header(b'Server:\r\n')
        self.assertEqual(req.headers, [http.HttpHeader('Server', '')])

        req.headers = []
        req._parse_header(b'Server: \r\n')
        self.assertEqual(req.headers, [http.HttpHeader('Server', '')])

        req.headers = []
        with self.assertRaises(http.BadHttpHeaderError):
            req._parse_header(b': pyx\r\n')

        req.headers = []
        with self.assertRaises(http.BadHttpHeaderError):
            req._parse_header(b' : pyx')


class TestHttpResponse(unittest.TestCase):
    def test_write(self):
        resp = http.HttpResponse(200, None)
        resp.headers = [
            http.HttpHeader('Server', 'Pyx'),
            http.HttpHeader('Connection', 'keep-alive')
        ]
        self.assertEqual(resp.write(),
                         ['HTTP/1.1 200 OK',
                          'Server: Pyx',
                          'Connection: keep-alive',
                          '\r\n'])
        self.assertEqual(str(resp),
                         'HTTP/1.1 200 OK\r\n'
                         'Server: Pyx\r\n'
                         'Connection: keep-alive\r\n'
                         '\r\n')


class DummyResource(http.UrlResource):
    def __getitem__(self, key):
        if key == 'hello':
            return self
        elif key == "static":
            return http.StaticRootResource('.')
        else:
            raise http.HttpError(404, '{} not found'.format(key))


class TestUrlResource(unittest.TestCase):
    def test_dispatch(self):
        res = DummyResource()
        self.assertEqual(res.dispatch(''), res)
        self.assertEqual(res.dispatch('/'), res)
        self.assertEqual(res.dispatch('/hello'), res)

        with self.assertRaises(http.HttpError):
            res.dispatch('/does/not/exist')

        sres = res.dispatch('/static')
        self.assertEqual(sres.root, '.')
        self.assertEqual(sres._build_real_path(), '.')

        sres = res.dispatch('/static/')
        self.assertEqual(sres._build_real_path(), '.')

        sres = res.dispatch('/static/some/path')
        self.assertEqual(sres._build_real_path(), './some/path')


class TestStaticRootResource(unittest.TestCase):
    def test_build_real_path(self):
        res = http.StaticRootResource('local_root')
        res = res.dispatch('/some/long/path/where/ever/it/leads/')
        self.assertEqual(res._build_real_path(),
                         'local_root/some/long/path/where/ever/it/leads')

        res = http.StaticRootResource('local_root')
        res = res.dispatch('/some/../dangerous/path')
        self.assertEqual(res._build_real_path(),
                         'local_root/dangerous/path')

        res = http.StaticRootResource('local_root')
        res = res.dispatch('/some/../../dangerous/path')
        self.assertEqual(res._build_real_path(),
                         'local_root/dangerous/path')

        res = http.StaticRootResource('local_root')
        res = res.dispatch('/some/%2e%2e%2f%2e%2e/dangerous/path')
        self.assertEqual(res._build_real_path(),
                         'local_root/dangerous/path')
