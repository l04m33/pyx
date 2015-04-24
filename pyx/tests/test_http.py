import unittest
import unittest.mock as mock
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


def create_dummy_connection():
    loop = asyncio.get_event_loop()

    reader = asyncio.StreamReader(loop=loop)

    @asyncio.coroutine
    def dummy_drain():
        yield from asyncio.sleep(0.001)
    writer = mock.Mock(spec=asyncio.StreamWriter)
    writer.attach_mock(mock.Mock(wraps=dummy_drain), 'drain')

    conn = http.HttpConnection(reader, writer)
    return conn


def create_dummy_request():
    conn = create_dummy_connection()
    req = http.HttpRequest(conn)
    return req


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

        with self.assertRaises(http.BadHttpRequestError):
            req._parse_req_line(b'GET /\r\n')

        with self.assertRaises(http.BadHttpRequestError):
            req._parse_req_line(b'GET / GARBAGE\r\n')

        req._parse_req_line(b'GET / HTTP/1\r\n')
        self.assertEqual(req.version, (1, 0))

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
        req._parse_header(b'Host: some.badasshost.com:8080\r\n')
        self.assertEqual(req.headers, [http.HttpHeader('Host', 'some.badasshost.com:8080')])

        with self.assertRaises(http.BadHttpHeaderError):
            req._parse_header(b': pyx\r\n')

        with self.assertRaises(http.BadHttpHeaderError):
            req._parse_header(b' : pyx')

        with self.assertRaises(http.BadHttpHeaderError):
            req._parse_header(b' \t : pyx')

    def test_parse(self):
        loop = asyncio.get_event_loop()
        conn = create_dummy_connection()

        reader = conn.reader
        reader.feed_data(
            b'GET /?q=p&s=t HTTP/1.1\r\n'
            b'Host: localhost\r\n'
            b'Connection: Keep-Alive\r\n'
            b'Pragma: Test\r\n'
            b' : Test\r\n'
            b'\r\n')

        req = loop.run_until_complete(http.HttpRequest.parse(conn))

        self.assertEqual(req.method, 'GET')
        self.assertEqual(req.path, '/')
        self.assertEqual(req.query, 'q=p&s=t')
        self.assertEqual(req.protocol, 'HTTP')
        self.assertEqual(req.version, (1, 1))
        self.assertEqual(req.headers,
                         [
                             http.HttpHeader('Host', 'localhost'),
                             http.HttpHeader('Connection', 'Keep-Alive'),
                             http.HttpHeader('Pragma', 'Test'),
                         ])

    def test_respond(self):
        req = create_dummy_request()

        req.version = (1, 1)
        resp = req.respond(200)
        self.assertEqual(resp.code, 200)
        self.assertEqual(resp.version, (1, 1))

        req.version = (1, 0)
        resp = req.respond(400)
        self.assertEqual(resp.code, 400)
        self.assertEqual(resp.version, (1, 0))


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

    def test_send(self):
        loop = asyncio.get_event_loop()
        req = create_dummy_request()
        resp = req.respond(200)
        self.assertEqual(resp.code, 200)
        self.assertFalse(req.responded)

        resp.headers = [
            http.HttpHeader('Server', 'Pyx'),
            http.HttpHeader('Content-Length', '100'),
            http.HttpHeader('Content-Type', 'text/plain'),
        ]
        loop.run_until_complete(resp.send())
        resp.connection.writer.write.assert_called_with(str(resp).encode())
        self.assertTrue(req.responded)

    def test_send_body(self):
        loop = asyncio.get_event_loop()
        req = create_dummy_request()
        resp = req.respond(200)

        loop.run_until_complete(resp.send())
        self.assertTrue(req.responded)

        loop.run_until_complete(resp.send_body(b'Yes, this is the body.'))
        resp.connection.writer.write.assert_called_with(b'Yes, this is the body.')

        loop.run_until_complete(resp.send_body('This is another string body.'))
        resp.connection.writer.write.assert_called_with(b'This is another string body.')


class DummyResource(http.UrlResource):
    def get_child(self, key):
        if key == 'hello':
            return self
        elif key == "static":
            return http.StaticRootResource('.')
        else:
            raise http.HttpError(404, '{} not found'.format(key))


class TestUrlResource(unittest.TestCase):
    def test_traverse(self):
        res = DummyResource()
        self.assertEqual(res.traverse(''), res)
        self.assertEqual(res.traverse('/'), res)
        self.assertEqual(res.traverse('/hello'), res)

        with self.assertRaises(http.HttpError):
            res.traverse('/does/not/exist')

        sres = res.traverse('/static')
        self.assertEqual(sres.root, '.')
        self.assertEqual(sres._build_real_path(), '.')

        sres = res.traverse('/static/')
        self.assertEqual(sres._build_real_path(), '.')

        sres = res.traverse('/static/some/path')
        self.assertEqual(sres._build_real_path(), './some/path')

    def test_not_implemented(self):
        res = http.UrlResource()

        with self.assertRaises(NotImplementedError):
            res.traverse('/hello')

        req = create_dummy_request()
        with self.assertRaises(NotImplementedError):
            res.handle_request(req)

class TestStaticRootResource(unittest.TestCase):
    def test_build_real_path(self):
        res = http.StaticRootResource('local_root')
        res = res.traverse('/some/long/path/where/ever/it/leads/')
        self.assertEqual(res._build_real_path(),
                         'local_root/some/long/path/where/ever/it/leads')

        res = http.StaticRootResource('local_root')
        res = res.traverse('/some/../dangerous/path')
        self.assertEqual(res._build_real_path(),
                         'local_root/dangerous/path')

        res = http.StaticRootResource('local_root')
        res = res.traverse('/some/../../dangerous/path')
        self.assertEqual(res._build_real_path(),
                         'local_root/dangerous/path')

        res = http.StaticRootResource('local_root')
        res = res.traverse('/some/%2e%2e%2f%2e%2e/dangerous/path')
        self.assertEqual(res._build_real_path(),
                         'local_root/dangerous/path')
