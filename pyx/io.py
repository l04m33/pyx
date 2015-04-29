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
        try:
            res = self._fileobj.read(n)
        except (BlockingIOError, InterruptedError):
            self._loop.add_reader(self._fileobj.fileno(),
                                  self._read_ready,
                                  future, n, total)
            return
        except Exception as exc:
            future.set_exception(exc)
            return

        if not res:     # EOF
            future.set_result(bytes(self._rbuffer))
            return

        self._rbuffer.extend(res)

        if total > 0:
            more_to_go = total - len(self._rbuffer)
            if more_to_go <= 0:  # enough
                res, self._rbuffer = self._rbuffer[:n], self._rbuffer[n:]
                future.set_result(bytes(res))
            else:
                more_to_go = min(self.DEFAULT_BLOCK_SIZE, more_to_go)
                self._loop.add_reader(self._fileobj.fileno(),
                                      self._read_ready,
                                      future, more_to_go, total)
        else:   # total < 0
            self._loop.add_reader(self._fileobj.fileno(),
                                  self._read_ready,
                                  future, self.DEFAULT_BLOCK_SIZE, total)

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
        try:
            res = self._fileobj.write(data)
        except (BlockingIOError, InterruptedError):
            self._loop.add_writer(self._fileobj.fileno(),
                                  self._write_ready,
                                  future, data, written)
            return
        except Exception as exc:
            future.set_exception(exc)
            return

        if res < len(data):
            data = data[res:]
            self._loop.add_writer(self._fileobj.fileno(),
                                  self._write_ready,
                                  future, data, written + res)
        else:
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


def sendfile_async(out_f, in_f, offset, nbytes, loop=None):
    def _get_fileno(f):
        if hasattr(f, 'fileno'):
            f = f.fileno()
        elif not isinstance(f, int):
            raise TypeError('Expected {}, but got {}'.format(int, type(f)))
        return f

    out_f = _get_fileno(out_f)
    in_f = _get_fileno(in_f)
    loop = loop or asyncio.get_event_loop()
    future = asyncio.Future(loop=loop)

    def _write_cb():
        try:
            res = os.sendfile(out_f, in_f, offset, nbytes)
        except (BlockingIOError, InterruptedError):
            loop.add_writer(out_f, _write_cb)
        except Exception as exc:
            future.set_exception(exc)
        else:
            future.set_result(res)

    _write_cb()

    return future

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
