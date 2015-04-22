import asyncio
import fcntl
import os
import ctypes
import errno
import io


class AsyncFileWrapper:
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
