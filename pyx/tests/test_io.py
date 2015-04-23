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


class TestBufferedReader(unittest.TestCase):
    def test_read(self):
        loop = asyncio.get_event_loop()
        sr = asyncio.StreamReader(loop=loop)
        br = io.BufferedReader(sr)

        sr.feed_data(b'test data 1\r\n')
        sr.feed_data(b'test data 2\r\n')
        sr.feed_data(b'test data 3')
        data = loop.run_until_complete(br.read(4))
        self.assertEqual(data, b'test')

        br.put(data)
        br.put(b'dest ')
        data = loop.run_until_complete(br.read(2))
        self.assertEqual(data, b'de')
        data = loop.run_until_complete(br.read(8))
        self.assertEqual(data, b'st test ')
        data = loop.run_until_complete(br.read(7))
        self.assertEqual(data, b'data 1\r')

        br.put(data)
        data = loop.run_until_complete(br.readline())
        self.assertEqual(data, b'data 1\r\n')

        br.put(data + b'padding ')
        data = loop.run_until_complete(br.readline())
        self.assertEqual(data, b'data 1\r\n')

        data = loop.run_until_complete(br.readline())
        self.assertEqual(data, b'padding test data 2\r\n')

        sr.feed_eof()
        data = loop.run_until_complete(br.readline())
        self.assertEqual(data, b'test data 3')

    def test_read_all(self):
        loop = asyncio.get_event_loop()
        sr = asyncio.StreamReader(loop=loop)
        br = io.BufferedReader(sr)

        sr.feed_data(b'test data 1\r\n')
        sr.feed_data(b'test data 2')
        br.put(b'test data 0\r\n')

        sr.feed_eof()
        data = loop.run_until_complete(br.read())
        self.assertEqual(data,
                         b'test data 0\r\n'
                         b'test data 1\r\n'
                         b'test data 2')

    def test_read_exactly(self):
        loop = asyncio.get_event_loop()
        sr = asyncio.StreamReader(loop=loop)
        br = io.BufferedReader(sr)

        sr.feed_data(b'test data 1')
        sr.feed_eof()
        with self.assertRaises(asyncio.IncompleteReadError):
            loop.run_until_complete(br.readexactly(12))


class TestLengthReader(unittest.TestCase):
    def test_read(self):
        loop = asyncio.get_event_loop()
        sr = asyncio.StreamReader(loop=loop)
        br = io.BufferedReader(sr)

        sr.feed_data(
            b'1 2 3 4 5 6 \r\n'
            b'padding\r\n'
            b'more padding')

        lr = io.LengthReader(br, 4)
        data = loop.run_until_complete(lr.read())
        self.assertEqual(data, b'1 2 ')
        data = loop.run_until_complete(lr.read(2))
        self.assertEqual(data, b'')

        lr = io.LengthReader(br, 4)
        data = loop.run_until_complete(lr.read(2))
        self.assertEqual(data, b'3 ')

        lr = io.LengthReader(br, 10)
        data = loop.run_until_complete(lr.readline())
        self.assertEqual(data, b'4 5 6 \r\n')

        lr = io.LengthReader(br, 4)
        data = loop.run_until_complete(lr.readline())
        self.assertEqual(data, b'padd')

    def test_read_exactly(self):
        loop = asyncio.get_event_loop()
        sr = asyncio.StreamReader(loop=loop)
        br = io.BufferedReader(sr)

        sr.feed_data(b'1 2 3 4 5 6 ')

        lr = io.LengthReader(br, 4)
        with self.assertRaises(asyncio.IncompleteReadError):
            loop.run_until_complete(lr.readexactly(5))

        lr = io.LengthReader(br, 4)
        data = loop.run_until_complete(lr.readexactly(-1))
        self.assertEqual(data, b'')

        data = loop.run_until_complete(lr.readexactly(4))
        self.assertEqual(data, b'3 4 ')

        with self.assertRaises(asyncio.IncompleteReadError):
            loop.run_until_complete(lr.readexactly(1))

    def test_put(self):
        loop = asyncio.get_event_loop()
        sr = asyncio.StreamReader(loop=loop)
        br = io.BufferedReader(sr)

        sr.feed_data(b'1 2 3 4 5 6 ')

        lr = io.LengthReader(br, 10)
        data = loop.run_until_complete(lr.read(4))
        self.assertEqual(data, b'1 2 ')
        data2 = loop.run_until_complete(lr.read(4))
        self.assertEqual(data2, b'3 4 ')

        lr.put(data)
        lr.put(data2)
        data = loop.run_until_complete(lr.read())
        self.assertEqual(data, b'3 4 1 2 5 ')
