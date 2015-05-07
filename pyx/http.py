"""
Routines & classes that are related to HTTP protocol processing.

A basic HTTP server can be assembled in a simple way::

    import asyncio
    from pyx.http import (HttpHeader, HttpConnectionCB)

    @asyncio.coroutine
    def req_cb(req):
        resp = req.respond(200)
        resp.headers.append(HttpHeader('Content-Length', 5))
        resp.headers.append(HttpHeader('Content-Type', 'text/plain'))
        yield from resp.send()
        yield from resp.send_body(b'hello')

    loop = asyncio.get_event_loop()

    conn_cb = HttpConnectionCB(req_cb)
    starter = asyncio.start_server(conn_cb, '127.0.0.1', 8080, loop=loop)
    _server = loop.run_until_complete(starter)

    loop.run_forever()

"""


import asyncio
import collections
import urllib
import mimetypes
import os
import traceback
from .log import logger
from .io import (AsyncFile, sendfile_async, BoundaryReader)


__all__ = ['BadHttpRequestError', 'BadHttpHeaderError', 'HttpError',
           'HttpHeader', 'parse_http_header', 'get_kv', 'get_first_kv',
           'HttpConnection', 'HttpMessage', 'HttpRequest', 'HttpResponse',
           'DefaultHttpErrorHandler', 'default_error_page',
           'HttpRequestCB', 'HttpConnectionCB',
           'UrlResource', 'StaticRootResource', 'methods',
           'parse_multipart_formdata',
           'status_messages', ]


class BadHttpRequestError(Exception):
    """Raised when the HTTP request is invalid."""


class BadHttpHeaderError(Exception):
    """Raised when there is an invalid HTTP header.

    ``HttpRequest.parse`` catches and log this exception by default. The invalid
    header will then be skipped.
    """


class HttpError(Exception):
    """Raised when the request handling code wants to generate an HTTP error
    code (404, 500, etc.).

    The ``code`` argument is an integer standing for standard HTTP status code.
    The optional ``msg`` argument can be used to provide more info in logs.
    """

    def __init__(self, code, msg=''):
        super().__init__(msg)
        self.code = code

    def __str__(self):
        msg = super().__str__()
        if msg:
            return '{}({}, {})'.format(self.__class__.__name__,
                                       self.code, repr(msg))
        else:
            return '{}({})'.format(self.__class__.__name__, self.code)


status_messages = {
    200: "OK",
    303: "See Other",
    400: "Bad Request",
    404: "Not Found",
    500: "Internal Error",
    501: "Not Implemented",
}


class HttpConnection:
    """Connection level data & operations.

    ``reader`` should be an ``asyncio.StreamReader``, or implementing the same
    interface.
    ``writer`` should be an ``asyncio.StreamWriter``, or implementing the same
    interface.
    """

    def __init__(self, reader, writer):
        self._reader = reader
        self._writer = writer
        self._closed = False

    @property
    def closed(self):
        """True if ``close()`` has been called."""
        return self._closed

    def close(self):
        """Close this connection."""
        logger('HttpConnection').debug('Closing connection....')
        self.writer.close()
        self._closed = True

    @property
    def reader(self):
        """The reader for this connection."""
        return self._reader

    @reader.setter
    def reader(self, new_reader):
        """Sets a new reader for this connection."""
        self._reader = new_reader

    @property
    def writer(self):
        """The writer for this connection."""
        return self._writer

    @writer.setter
    def writer(self, new_writer):
        """Sets a new writer for this connection."""
        self._writer = new_writer


HttpHeader = collections.namedtuple('HttpHeader', ['key', 'value'])


def parse_http_header(header_line):
    """Parse an HTTP header from a string, and return an ``HttpHeader``.

    ``header_line`` should only contain one line.
    ``BadHttpHeaderError`` is raised if the string is an invalid header line.
    """

    header_line = header_line.decode().strip()
    col_idx = header_line.find(':')

    if col_idx < 1:
        raise BadHttpHeaderError('Bad header: {}'.format(repr(header_line)))

    key = header_line[0:col_idx].strip()
    value = header_line[(col_idx+1):].strip()
    return HttpHeader(key=key, value=value)


def get_kv(kv_list, key):
    upper_key = key.upper()
    vlist = []
    for i in kv_list:
        if i.key.upper() == upper_key:
            vlist.append(i.value)
    return vlist


def get_first_kv(kv_list, key):
    upper_key = key.upper()
    for i in kv_list:
        if i.key.upper() == upper_key:
            return i.value
    return None


class HttpMessage:
    """Base class for requests and responses."""

    def __init__(self, conn):
        self.connection = conn
        self.headers = []

    def get_header(self, key):
        """Search for a header, and return the values as a list.

        ``key`` is case-insensitive.
        Note that some HTTP requests may contain multiple headers with the same
        name, so this method returns a lisst.
        """
        return get_kv(self.headers, key)

    def get_first_header(self, key):
        """Search for a header, and return the first encountered value.

        ``key`` is case-insensitive.
        """
        return get_first_kv(self.headers, key)

    def write_headers(self):
        """Construct headers in string form, and return a list containing each
        line of header strings.
        """
        hlist = []
        for h in self.headers:
            hlist.append("{}: {}".format(h.key, h.value))
        return hlist


class HttpRequest(HttpMessage):
    """An HTTP request.

    You should use the class method ``HttpRequest.parse(conn)`` to read a
    request from the connection, instead of invoking the constructor of this
    class directly.

    When successfully parsed, an ``HttpRequest`` consists of these data
    members:

        ``method``:   The Http method.
        ``path``:     The requesting path from the URL.
        ``query``:    The query string from the URL, None if no query exists.
        ``version``:  HTTP version, as a tuple (major, minor)
        ``protocol``: Should always be "HTTP"
        ``headers``:  HTTP headers for this request.
    """

    def __init__(self, conn):
        super().__init__(conn)
        self._responded = False

    def _parse_req_line(self, req_line):
        req_line = req_line.decode().strip()
        # Shortcut for client disconnection
        if len(req_line) == 0:
            raise BadHttpRequestError('Bad request line: {}'.format(repr(req_line)))

        comps = req_line.split(' ')

        if len(comps) != 3:
            raise BadHttpRequestError('Bad request line: {}'.format(repr(req_line)))

        self.method = comps[0].upper()

        qmark_idx = comps[1].find('?')
        if qmark_idx < 0:
            self.path = comps[1]
            self.query = None
        else:
            self.path = comps[1][0:qmark_idx]
            self.query = comps[1][(qmark_idx+1):]

        vmark_idx = comps[2].find('/')
        self.version = (1, 1)
        if vmark_idx < 0:
            raise BadHttpRequestError('Bad request line: {}'.format(repr(req_line)))
        else:
            self.protocol = comps[2][0:vmark_idx].upper()
            vstr = comps[2][(vmark_idx+1):]
            vpoint_idx = vstr.find('.')
            if vpoint_idx < 0:
                majorVersion = int(vstr, 10)
                minorVersion = 0
            else:
                majorVersion = int(vstr[0:vpoint_idx], 10)
                minorVersion = int(vstr[(vpoint_idx+1):], 10)
            self.version = (majorVersion, minorVersion)

    def _parse_header(self, header_line):
        self.headers.append(parse_http_header(header_line))

    def respond(self, code):
        """Starts a response.

        ``code`` is an integer standing for standard HTTP status code.

        This method will automatically adjust the response to adapt to request
        parameters, such as "Accept-Encoding" and "TE".
        """

        # TODO: respect encodings etc. in the request
        resp = HttpResponse(code, self.connection)
        resp.request = self
        if hasattr(self, 'version'):
            resp.version = self.version
        return resp

    @property
    def responded(self):
        """True if ``HttpResponse.send(...)`` is called."""
        return self._responded

    @responded.setter
    def responded(self, value):
        assert (type(value) is bool)
        self._responded = value

    @classmethod
    @asyncio.coroutine
    def parse(cls, conn):
        """Read a request from the HTTP connection ``conn``.

        May raise ``BadHttpRequestError``.
        """

        req = cls(conn)
        req_line = yield from conn.reader.readline()
        logger('HttpRequest').debug('req_line = %r', req_line)
        req._parse_req_line(req_line)

        header_line = yield from conn.reader.readline()
        while len(header_line) > 0 and header_line != b'\r\n':
            try:
                req._parse_header(header_line)
            except BadHttpHeaderError as e:
                # Tolerating 'minor' mistakes
                logger('HttpRequest').debug(traceback.format_exc())
            header_line = yield from conn.reader.readline()
        return req


class HttpResponse(HttpMessage):
    """An HTTP response.

    You should use ``HttpRequest.respond(...)`` to start a response, instead
    of invoking the constructor of this class directly.
    """

    def __init__(self, code, conn):
        super().__init__(conn)
        self.code = code
        self.protocol = 'HTTP'
        self.version = (1, 1)
        self.headers = [HttpHeader('Server', 'Pyx 0.1.0')]

    def write(self):
        """Construct the response header.

        The return value is a list containing the whole response header, with
        each line as a list element.
        """

        slist = []
        slist.append('{}/{}.{} {} {}'.format(
                        self.protocol,
                        self.version[0],
                        self.version[1],
                        self.code,
                        status_messages[self.code]))
        slist.extend(self.write_headers())
        slist.append('\r\n')
        return slist

    def __str__(self):
        return '\r\n'.join(self.write())

    @asyncio.coroutine
    def send(self):
        """Send the response header, including the status line and all the
        HTTP headers.
        """

        if hasattr(self, 'request'):
            self.request.responded = True
        self.connection.writer.write(str(self).encode())
        yield from self.connection.writer.drain()

    @asyncio.coroutine
    def send_body(self, data):
        """Send the response body.

        ``data`` should be a bytes-like object or a string.
        """

        if type(data) is str:
            data = data.encode()
        self.connection.writer.write(data)
        yield from self.connection.writer.drain()


def default_error_page(code):
    """The default template for error pages."""

    return """
<html>
    <head>
        <meta http-equiv="content-type" content="text/html; charset=utf-8">
        <title>Error: {0}</title>
        <style>
        </style>
    </head>
    <body>
        <h1>Error</h1>
        <p>{0} - {1}</p>
    </body>
</html>""".format(code, status_messages[code])


class DefaultHttpErrorHandler:
    """Display an error page when an ``HttpError`` is detected.

    ``error_page`` should be a function which accepts the HTTP status code
    and returns the content of the error page. See ``default_error_page``.
    """

    def __init__(self, error_page=default_error_page):
        self._gen_error_page = error_page

    @asyncio.coroutine
    def __call__(self, err, req):
        resp = req.respond(err.code)
        content = self._gen_error_page(err.code)
        resp.headers.append(HttpHeader('Content-Length', len(content)))
        resp.headers.append(HttpHeader('Content-Type', 'text/html'))
        yield from resp.send()
        yield from resp.send_body(content)


_default_error_handler = DefaultHttpErrorHandler()


class HttpRequestCB:
    """Default request callback for ``HttpConnectionCB``.

    This callback routine handles path traversal and exceptions raised by
    ``UrlResource`` objects.

    ``root_factory`` is a factory callable which produces a root object for
    path traversal.
    The optional argument ``error_handler`` should be a callable that can
    handle ``HttpError``. See ``DefaultHttpErrorHandler``.
    """

    def __init__(self, root_factory, error_handler=_default_error_handler):
        self._root_factory = root_factory
        self._error_handler = error_handler

    @asyncio.coroutine
    def _generate_500_and_stop(self, req, trace_msg):
        logger('HttpRequestCB').debug(trace_msg)
        if not req.responded:
            e = HttpError(500, trace_msg)
            try:
                yield from self._error_handler(e, req)
            except:
                logger('HttpRequestCB').debug(traceback.format_exc())
        req.connection.close()

    @asyncio.coroutine
    def _handle_http_error(self, req, exc, trace_msg):
        if not req.responded:
            try:
                yield from self._error_handler(exc, req)
            except:
                logger('HttpRequestCB').debug(trace_msg)
                req.connection.close()

    @asyncio.coroutine
    def __call__(self, req):
        try:
            res = self._root_factory(req)
            res = res.traverse(req.path)
        except HttpError as e:
            yield from self._handle_http_error(req, e, traceback.format_exc())
            return
        except:
            yield from self._generate_500_and_stop(req, traceback.format_exc())
            return

        try:
            yield from res._do_handle_request(req)
        except HttpError as e:
            yield from self._handle_http_error(req, e, traceback.format_exc())
        except:
            yield from self._generate_500_and_stop(req, traceback.format_exc())


class HttpConnectionCB:
    """Default callback for use with ``asyncio.start_server(...)``.

    ``req_cb`` should be a callable for handling HTTP requests. See
    ``HttpRequestCB``.
    """
    def __init__(self, req_cb):
        self._request_cb = req_cb

    @asyncio.coroutine
    def __call__(self, reader, writer):
        conn = HttpConnection(reader, writer)
        while not conn.closed:
            try:
                req = yield from HttpRequest.parse(conn)
            except Exception as e:
                logger('HttpConnectionCB').debug(traceback.format_exc())
                conn.close()
                break

            yield from self._request_cb(req)

            if req.version < (1, 1):
                conn.close()
            else:
                conn_header = req.get_first_header('Connection')
                if (conn_header is None) or \
                        (conn_header.upper() == 'KEEP-ALIVE'):
                    continue
                else:
                    conn.close()


class UrlResource:
    """Base class for path traversal objects.

    You should write subclasses and implement the methods ``get_child`` and
    ``handle_request``, instead of using this class directly.
    """
    def get_child(self, key):
        raise NotImplementedError('UrlResource.get_child(...) not implemented')

    def handle_request(self, req):
        raise NotImplementedError('UrlResource.get_child(...) not implemented')

    @asyncio.coroutine
    def _do_handle_request(self, req):
        if isinstance(self.handle_request, _HandleRequestDict):
            try:
                handler = self.handle_request[req.method]
            except KeyError:
                raise HttpError(501, 'Method {} not implemented for {}'.format(
                                        req.method, self.__class__))
            yield from handler(self, req)
        else:
            yield from self.handle_request(req)

    def traverse(self, path):
        segs = path.split('/')
        res = self
        for s in segs:
            if len(s) > 0:
                logger('UrlResource').debug("Traversing to resource %r", s)
                res = res.get_child(s)
        return res


class _HandleRequestDict(dict):
    def methods(self, method_list):
        assert isinstance(method_list, list) and len(method_list) > 0

        def deco(handler):
            for m in method_list:
                self[m.upper()] = handler
            return self

        return deco


def methods(method_list):
    """A decorator to mark HTTP methods a resource can handle.

    For example::

        class SomeRes(UrlResource):
            ...
            @methods(['GET', 'HEAD'])
            def handle_request(self, req):
                ...
            @handle_request.methods(['POST'])
            def handle_post(self, req):
                ...

    In this case, GET and HEAD requests will be dispatched to
    ``handle_request``, and POST requests will be dispatched to
    ``handle_post``. All other request methods will cause a
    *501 Not Implemented* error.
    """

    assert isinstance(method_list, list) and len(method_list) > 0

    def deco(handler):
        d = _HandleRequestDict()
        for m in method_list:
            d[m.upper()] = handler
        return d

    return deco


class StaticRootResource(UrlResource):
    """A resource class for serving static files.

    ``local_root`` is the local directory for your static files.
    """

    def __init__(self, local_root):
        super().__init__()
        self.root = local_root
        self.path = []

    def get_child(self, key):
        unquoted_key = urllib.parse.unquote(key)
        segs = unquoted_key.split('/')
        for s in segs:
            if s == '..':
                if len(self.path) > 0:
                    self.path.pop()
            else:
                self.path.append(s)
        return self

    def _build_real_path(self):
        return os.path.join(self.root, *self.path)

    @methods(['GET'])
    @asyncio.coroutine
    def handle_request(self, req):
        path = self._build_real_path()

        logger('StaticRootResource').debug('path = %r', path)

        if os.path.isfile(path):

            with AsyncFile(filename=path) as af:
                resp = req.respond(200)

                file_size = af.stat().st_size
                resp.headers.append(HttpHeader('Content-Length', file_size))
                mimetype, _encoding = mimetypes.guess_type(path)
                if mimetype is not None:
                    resp.headers.append(HttpHeader('Content-Type', mimetype))

                yield from resp.send()
                sock = resp.connection.writer.get_extra_info('socket')
                yield from sendfile_async(sock, af, None, file_size)
        else:
            raise HttpError(404, '{} not found'.format(repr(path)))


@asyncio.coroutine
def parse_multipart_formdata(reader, boundary, cb):
    """Read data from ``reader`` and parse multipart/form-data fields.

    ``boundary`` is the multipart/form-data boundary.
    ``cb`` is a callable that will be called as ``cb(headers, reader)`` to
    handle the parsed field.
    """

    breader = BoundaryReader(reader, boundary)
    # The BoundaryReader expects a new line before the boundary string,
    # We make sure the new line exists
    breader.put(b'\r\n')
    # Clean up garbage before the first boundary
    _dummy_data = yield from breader.read()
    logger('parse_multipart_formdata').debug('_dummy_data = %r', _dummy_data)
    del _dummy_data

    breader = BoundaryReader(reader, boundary)
    line = yield from breader.readline()
    while line:
        headers = []
        while line.strip():
            header = parse_http_header(line)
            headers.append(header)
            line = yield from breader.readline()

        yield from cb(headers, breader)

        yield from breader.read()
        breader = BoundaryReader(reader, boundary)
        line = yield from breader.readline()
