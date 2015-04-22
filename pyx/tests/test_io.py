import unittest
import tempfile
import asyncio
import pyx.io as io


def create_dummy_file():
    f = tempfile.NamedTemporaryFile()
    f.write(b'dummy content\r\n')
    f.write(b'dummy content 2\r\n')
    f.write(b'dummy content 3\r\n')
    f.write(b'dummy content 4\r\n')
    f.seek(0)
    return f


class TestAsyncFileWrapper(unittest.TestCase):
    def test_read(self):
        loop = asyncio.get_event_loop()
        f = create_dummy_file()

        with io.AsyncFileWrapper(fileobj=f) as af:
            data = loop.run_until_complete(af.read(15))
            self.assertEqual(data, b'dummy content\r\n')
            data = loop.run_until_complete(af.read(17))
            self.assertEqual(data, b'dummy content 2\r\n')
            self.assertEqual(af.tell(), 32)
            data = loop.run_until_complete(af.read())
            self.assertEqual(data, b'dummy content 3\r\ndummy content 4\r\n')

        self.assertTrue(f.closed)

    def test_write(self):
        loop = asyncio.get_event_loop()
        f = create_dummy_file()
        f.seek(0, 2)

        with io.AsyncFileWrapper(fileobj=f) as af:
            written = loop.run_until_complete(af.write(b'new data\r\n'))
            self.assertEqual(written, 10)
            self.assertEqual(af.tell(), 76)
            f.seek(0)
            data = f.read()
            self.assertEqual(data,
                             b'dummy content\r\n'
                             b'dummy content 2\r\n'
                             b'dummy content 3\r\n'
                             b'dummy content 4\r\n'
                             b'new data\r\n')

        self.assertTrue(f.closed)
