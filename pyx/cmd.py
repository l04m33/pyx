import asyncio
import logging
from .log import logger
from .http import (HttpConnectionCB, HttpRequestCB, StaticRootResource)


def main():
    # TODO: parse commandline options
    logging.basicConfig(level=logging.DEBUG)

    loop = asyncio.get_event_loop()

    def root_factory(req):
        return StaticRootResource('.')

    req_cb = HttpRequestCB(root_factory)
    conn_cb = HttpConnectionCB(req_cb)

    starter = asyncio.start_server(conn_cb, '127.0.0.1', 8080, loop=loop)
    server = loop.run_until_complete(starter)

    logger().debug('Server serving at 127.0.0.1:8080')

    try:
        loop.run_forever()
    except KeyboardInterrupt:
        pass

    server.close()
    loop.run_until_complete(server.wait_closed())
    loop.close()


if __name__ == '__main__':
    main()
