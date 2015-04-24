import asyncio
import fcntl
import os
import ctypes
import errno
import io


class AsyncFile:
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

        self.fileobj = fileobj

        if loop is None:
            loop = asyncio.get_event_loop()
        self.loop = loop
        self.rbuffer = bytearray()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def seek(self, offset, whence=None):
        if whence is None:
            return self.fileobj.seek(offset)
        else:
            return self.fileobj.seek(offset, whence)

    def tell(self):
        return self.fileobj.tell()

    def read_ready(self, future, n, total):
        try:
            res = self.fileobj.read(n)
        except Exception as exc:
            future.set_exception(exc)
            return

        if res is None:  # Blocked
            self.read_handle = \
                self.loop.call_soon(self.read_ready, future, n, total)
            return

        if not res:     # EOF
            future.set_result(bytes(self.rbuffer))
            return

        self.rbuffer.extend(res)

        if total > 0:
            more_to_go = total - len(self.rbuffer)
            if more_to_go <= 0:  # enough
                res, self.rbuffer = self.rbuffer[:n], self.rbuffer[n:]
                future.set_result(bytes(res))
            else:
                more_to_go = min(self.DEFAULT_BLOCK_SIZE, more_to_go)
                self.read_handle = \
                    self.loop.call_soon(self.read_ready, future,
                                        more_to_go, total)
        else:   # total < 0
            self.read_handle = \
                self.loop.call_soon(self.read_ready, future,
                                    self.DEFAULT_BLOCK_SIZE, total)

    @asyncio.coroutine
    def read(self, n=-1):
        future = asyncio.Future(loop=self.loop)

        if n == 0:
            future.set_result(b'')
            return future
        elif n < 0:
            self.rbuffer.clear()
            self.read_handle = \
                self.loop.call_soon(self.read_ready, future,
                                    self.DEFAULT_BLOCK_SIZE, n)
        else:
            self.rbuffer.clear()
            read_block_size = min(self.DEFAULT_BLOCK_SIZE, n)
            self.read_handle = \
                self.loop.call_soon(self.read_ready, future,
                                    read_block_size, n)

        return future

    def write_ready(self, future, data, written):
        try:
            res = self.fileobj.write(data)
        except io.BlockingIOError:
            self.write_handle = \
                self.loop.call_soon(self.write_ready, future, data, written)
            return
        except Exception as exc:
            future.set_exception(exc)
            return

        if res < len(data):
            data = data[res:]
            self.write_handle = \
                self.loop.call_soon(self.write_ready, future,
                                    data, written + res)
        else:
            future.set_result(written + res)

    @asyncio.coroutine
    def write(self, data):
        future = asyncio.Future(loop=self.loop)

        if len(data) > 0:
            self.write_handle = \
                self.loop.call_soon(self.write_ready, future, data, 0)
        else:
            future.set_result(0)

        return future

    def close(self):
        self.fileobj.close()
        if hasattr(self, 'read_handle'):
            self.read_handle.cancel()
        if hasattr(self, 'write_handle'):
            self.write_handle.cancel()


class BufferedMixin:
    def init_buffer(self):
        self._buffer = []

    def flush_buffer(self):
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
    def __init__(self, reader):
        self._reader = reader


class BufferedReader(BaseReader, BufferedMixin):
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
