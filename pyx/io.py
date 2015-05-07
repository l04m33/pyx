"""
I/O related routines & classes.

"""


import asyncio
import fcntl
import os
import ctypes
import errno
import io
from .log import logger


__all__ = ['AsyncFile', 'sendfile_async', 'BufferedMixin',
           'BaseReader', 'BufferedReader', 'LengthReader', 'BoundaryReader']


class AsyncFile:
    """A local file class for use with the ``asyncio`` module.

    ``loop`` should be the event loop in use.
    ``filename`` is the name of the file to be opened.
    ``fileobj`` should be a regular file-like object.
    ``mode`` is the open mode accepted by built-in function ``open``.

    If ``filename`` is specified, the named file will be opened. And if
    ``fileobj`` is specified, that file object will be used directly. You
    cannot specify both ``filename`` and ``fileobj``.

    This class can be used in a ``with`` statement.
    """

    DEFAULT_BLOCK_SIZE = 8192

    def __init__(self, loop=None, filename=None,
                 fileobj=None, mode='rb'):
        if (filename is None and fileobj is None) or \
                (filename is not None and fileobj is not None):
            raise RuntimeError('Confilicting arguments')

        if filename is not None:
            if 'b' not in mode:
                raise RuntimeError('Only binary mode is supported')
            fileobj = open(filename, mode=mode)
        elif 'b' not in fileobj.mode:
            raise RuntimeError('Only binary mode is supported')

        fl = fcntl.fcntl(fileobj, fcntl.F_GETFL)
        if fcntl.fcntl(fileobj, fcntl.F_SETFL, fl | os.O_NONBLOCK) != 0:
            if filename is not None:
                fileobj.close()
            errcode = ctypes.get_errno()
            raise OSError((errcode, errno.errorcode[errcode]))

        self._fileobj = fileobj

        if loop is None:
            loop = asyncio.get_event_loop()
        self._loop = loop
        self._rbuffer = bytearray()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def fileno(self):
        return self._fileobj.fileno()

    def seek(self, offset, whence=None):
        if whence is None:
            return self._fileobj.seek(offset)
        else:
            return self._fileobj.seek(offset, whence)

    def tell(self):
        return self._fileobj.tell()

    def _read_ready(self, future, n, total):
        if future.cancelled():
            self._loop.remove_reader(self._fileobj.fileno())
            return

        try:
            res = self._fileobj.read(n)
        except (BlockingIOError, InterruptedError):
            return
        except Exception as exc:
            self._loop.remove_reader(self._fileobj.fileno())
            future.set_exception(exc)
            return

        if not res:     # EOF
            self._loop.remove_reader(self._fileobj.fileno())
            future.set_result(bytes(self._rbuffer))
            return

        self._rbuffer.extend(res)

        if total > 0:
            more_to_go = total - len(self._rbuffer)
            if more_to_go <= 0:  # enough
                res, self._rbuffer = self._rbuffer[:n], self._rbuffer[n:]
                self._loop.remove_reader(self._fileobj.fileno())
                future.set_result(bytes(res))
            else:
                more_to_go = min(self.DEFAULT_BLOCK_SIZE, more_to_go)
                self._loop.add_reader(self._fileobj.fileno(),
                                      self._read_ready,
                                      future, more_to_go, total)
        else:   # total < 0
            # This callback is still registered with total < 0,
            # nothing to do here
            pass

    @asyncio.coroutine
    def read(self, n=-1):
        future = asyncio.Future(loop=self._loop)

        if n == 0:
            future.set_result(b'')
        else:
            try:
                res = self._fileobj.read(n)
            except (BlockingIOError, InterruptedError):
                if n < 0:
                    self._rbuffer.clear()
                    self._loop.add_reader(self._fileobj.fileno(),
                                          self._read_ready,
                                          future, self.DEFAULT_BLOCK_SIZE, n)
                else:
                    self._rbuffer.clear()
                    read_block_size = min(self.DEFAULT_BLOCK_SIZE, n)
                    self._loop.add_reader(self._fileobj.fileno(),
                                          self._read_ready,
                                          future, read_block_size, n)
            except Exception as exc:
                future.set_exception(exc)
            else:
                future.set_result(res)

        return future

    def _write_ready(self, future, data, written):
        if future.cancelled():
            self._loop.remove_writer(self._fileobj.fileno())
            return

        try:
            res = self._fileobj.write(data)
        except (BlockingIOError, InterruptedError):
            return
        except Exception as exc:
            self._loop.remove_writer(self._fileobj.fileno())
            future.set_exception(exc)
            return

        if res < len(data):
            data = data[res:]
            self._loop.add_writer(self._fileobj.fileno(),
                                  self._write_ready,
                                  future, data, written + res)
        else:
            self._loop.remove_writer(self._fileobj.fileno())
            future.set_result(written + res)

    @asyncio.coroutine
    def write(self, data):
        future = asyncio.Future(loop=self._loop)

        if len(data) == 0:
            future.set_result(0)
        else:
            try:
                res = self._fileobj.write(data)
            except (BlockingIOError, InterruptedError):
                self._loop.add_writer(self._fileobj.fileno(),
                                      self._write_ready,
                                      future, data, 0)
            except Exception as exc:
                future.set_exception(exc)
            else:
                future.set_result(res)

        return future

    def stat(self):
        return os.stat(self._fileobj.fileno(), follow_symlinks=True)

    def close(self):
        self._loop.remove_reader(self._fileobj.fileno())
        self._loop.remove_writer(self._fileobj.fileno())
        self._fileobj.close()


@asyncio.coroutine
def sendfile_async(out_f, in_f, offset, nbytes, loop=None):
    """The async version of ``os.sendfile(...)``.

    ``out_f`` and ``in_f`` can be any object with a ``fileno()`` method, but
    they must all be set to async mode beforehand.
    """
    loop = loop or asyncio.get_event_loop()

    if offset is None:
        while nbytes > 0:
            copied = yield from _sendfile_async(out_f, in_f,
                                                offset, nbytes, loop)
            nbytes -= copied
    else:
        total_size = offset + nbytes
        cur_offset = offset
        while cur_offset < total_size:
            copied = yield from _sendfile_async(out_f, in_f,
                                                cur_offset,
                                                total_size - cur_offset,
                                                loop)
            cur_offset += copied


def _sendfile_cb(future, out_f, in_f, offset, nbytes, loop):
    if future.cancelled():
        loop.remove_writer(out_f)
        return

    try:
        res = os.sendfile(out_f, in_f, offset, nbytes)
    except (BlockingIOError, InterruptedError):
        pass
    except Exception as exc:
        loop.remove_writer(out_f)
        future.set_exception(exc)
    else:
        loop.remove_writer(out_f)
        future.set_result(res)


@asyncio.coroutine
def _sendfile_async(out_f, in_f, offset, nbytes, loop):
    def _get_fileno(f):
        if hasattr(f, 'fileno'):
            f = f.fileno()
        elif not isinstance(f, int):
            raise TypeError('Expected {}, but got {}'.format(int, type(f)))
        return f

    out_f = _get_fileno(out_f)
    in_f = _get_fileno(in_f)
    future = asyncio.Future(loop=loop)

    try:
        res = os.sendfile(out_f, in_f, offset, nbytes)
    except (BlockingIOError, InterruptedError):
        loop.add_writer(out_f, _sendfile_cb,
                        future, out_f, in_f, offset, nbytes, loop)
    except Exception as exc:
        future.set_exception(exc)
    else:
        future.set_result(res)

    return future


class BufferedMixin:
    """A mixin providing buffered semantics."""

    def init_buffer(self):
        self._buffer = []

    def flush_buffer(self):
        self._buffer.reverse()
        buffered = b''.join(self._buffer)
        self._buffer = []
        return buffered

    def read_from_buffer(self, n):
        if n < 0:
            self._buffer.reverse()
            buffered = b''.join(self._buffer)
            self._buffer = []
            return (buffered, n)
        else:
            buffered = bytearray()
            while n > 0 and len(self._buffer) > 0:
                data = self._buffer.pop()
                if len(data) > n:
                    self._buffer.append(data[n:])
                    buffered.extend(data[0:n])
                    n = 0
                else:
                    buffered.extend(data)
                    n -= len(data)
            return (bytes(buffered), n)

    def put(self, data):
        self._buffer.append(data)


class BaseReader:
    """Base class for readers."""

    def __init__(self, reader):
        self._reader = reader


class BufferedReader(BaseReader, BufferedMixin):
    """A reader with buffered semantics."""

    def __init__(self, reader):
        super().__init__(reader)
        self.init_buffer()

    @asyncio.coroutine
    def readline(self):
        buffered = self.flush_buffer()
        nl_idx = buffered.find(b'\n')
        if nl_idx < 0:
            more_data = yield from self._reader.readline()
            return b''.join([buffered, more_data])
        else:
            self.put(buffered[(nl_idx+1):])
            return buffered[0:(nl_idx+1)]

    @asyncio.coroutine
    def read(self, n=-1):
        buffered, more = self.read_from_buffer(n)
        if more != 0:
            more_data = yield from self._reader.read(more)
            return b''.join([buffered, more_data])
        else:
            return buffered

    @asyncio.coroutine
    def readexactly(self, n):
        if n < 0:
            return b''

        buffered, more = self.read_from_buffer(n)
        if more != 0:
            more_data = yield from self._reader.readexactly(more)
            return b''.join([buffered, more_data])
        else:
            return buffered


class LengthReader(BaseReader):
    """A reader that reads at most ``length`` bytes."""

    def __init__(self, reader, length):
        super().__init__(reader)

        assert length >= 0, "Negative length not allowed"
        self._length = length
        self._remaining = length

    def put(self, data):
        self._reader.put(data)
        self._remaining += len(data)

    @asyncio.coroutine
    def readline(self):
        if self._remaining > 0:
            data = yield from self._reader.readline()
            if len(data) > self._remaining:
                self._reader.put(data[(self._remaining):])
                data = data[0:(self._remaining)]
                self._remaining = 0
                return data
            else:
                self._remaining -= len(data)
                return data
        else:
            return b''

    @asyncio.coroutine
    def read(self, n=-1):
        if self._remaining > 0:
            if n < 0:
                data = yield from self._reader.read(self._remaining)
                self._remaining -= len(data)
                return data
            else:
                data = yield from self._reader.read(min(self._remaining, n))
                self._remaining -= len(data)
                return data
        else:
            return b''

    @asyncio.coroutine
    def readexactly(self, n):
        if n < 0:
            return b''

        if self._remaining > 0:
            if self._remaining < n:
                data = yield from self._reader.readexactly(self._remaining)
                raise asyncio.IncompleteReadError(data, n)
            data = yield from self._reader.readexactly(n)
            self._remaining -= len(data)
            return data
        else:
            raise asyncio.IncompleteReadError(b'', n)


class BoundaryReader(BaseReader):
    """A reader that reads until the string ``boundary`` is encountered."""

    DEFAULT_BLOCK_SIZE = 8192

    def __init__(self, reader, boundary):
        super().__init__(reader)

        assert isinstance(boundary, bytes) and (len(boundary) > 0)
        self._boundary = b'\r\n--' + boundary
        self._hit_boundary = False

    def put(self, data):
        self._reader.put(data)

    @asyncio.coroutine
    def readline(self):
        if self._hit_boundary:
            return b''
        else:
            # The boundary starts with '\r\n', so it always come up between
            # two lines
            line = yield from self._reader.readline()
            line2 = yield from self._reader.readline()
            buf = b''.join([line, line2])

            bd_idx = buf.find(self._boundary)
            if bd_idx >= 0:
                self._hit_boundary = True
                _has_trailer, buf = \
                    yield from self._strip_boundary(buf, bd_idx)
                return buf

            self._reader.put(line2)
            return line

    @asyncio.coroutine
    def _read_next_block(self, n, buf):
        if n < 0:
            to_read = self.DEFAULT_BLOCK_SIZE
        else:
            remaining = max(n - len(buf), 0)
            if remaining < len(self._boundary) * 2:
                to_read = remaining + len(self._boundary)
            else:
                to_read = remaining

        return (yield from self._reader.read(to_read))

    @asyncio.coroutine
    def _strip_boundary(self, buf, bd_idx):
        # len(buf) is ALWAYS larger than 4, since len(self._boundary) > 4
        if bd_idx + len(self._boundary) > len(buf) - 4:
            # Also read the trailing '--\r\n', if any
            padding = yield from self._reader.read(4)
            buf.extend(padding)

        # See if these 4 bytes are '--\r\n' (the trailing sequence
        # of the ending boundary). If so, discard them together with
        # the boundary string
        search_idx = bd_idx + len(self._boundary)
        if buf[search_idx:(search_idx+4)] == b'--\r\n':
            self._reader.put(buf[(search_idx+4):])
            has_trailer = True
        elif buf[search_idx:(search_idx+2)] == b'\r\n':
            self._reader.put(buf[(search_idx+2):])
            has_trailer = False
        else:
            self._reader.put(buf[search_idx:])
            has_trailer = False

        return (has_trailer, buf[0:bd_idx])

    @asyncio.coroutine
    def read(self, n=-1):
        if self._hit_boundary:
            return b''

        else:
            buf = bytearray()
            # 1. Read at least (len(boundary) + 1) bytes
            data = yield from self._read_next_block(n, buf)
            # 2. If we hit EOF or have enough bytes already, stop.
            #    Here we want at least (n + len(boundary)) bytes in the buffer
            #    so that we can ensure the first n bytes are not part of the
            #    boundary
            while len(data) > 0 and \
                    (n < 0 or len(buf) < n + len(self._boundary)):
                buf.extend(data)
                # Only search the data we just read
                search_idx = \
                    max(len(buf) - len(data) - (len(self._boundary) - 1), 0)
                bd_idx = buf.find(self._boundary, search_idx)

                # 3. See if we have found the boundary string in buf.
                #    If the boundary is found, no more data shall be read.
                #    Else repeat step 1.
                if bd_idx >= 0:
                    self._hit_boundary = True
                    _has_trailer, buf = \
                        yield from self._strip_boundary(buf, bd_idx)
                    break

                data = yield from self._read_next_block(n, buf)

            if not self._hit_boundary:
                # We stopped before seeing the boundary string, and `data`
                # contains the last bunch of bytes we read
                buf.extend(data)

            if n >= 0 and len(buf) > n:
                self._reader.put(buf[n:])
                buf = buf[0:n]

            return bytes(buf)

    @asyncio.coroutine
    def readexactly(self, n):
        if n < 0:
            return b''

        buf = []
        readn = 0
        while readn < n:
            data = yield from self.read(n - readn)
            if not data:    # EOF
                raise asyncio.IncompleteReadError(b''.join(buf), n)
            readn += len(data)
            buf.append(data)
        return b''.join(buf)
