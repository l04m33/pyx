"""
Command line entry points.

"""


import asyncio
import logging
import argparse
from .log import logger
from .http import (HttpConnectionCB, HttpRequestCB, StaticRootResource)


__all__ = ['main']


def _parse_arguments():
    parser = argparse.ArgumentParser()
    parser.add_argument('-r', '--root',
                        help='Root dir to serve (default: .)',
                        default='.',
                        type=str)
    parser.add_argument('-b', '--bind',
                        help='Specify bind address (default: all interfaces)',
                        default='',
                        type=str)
    parser.add_argument('-p', '--port',
                        help='Which port to listen (default: 8000)',
                        default=8000,
                        type=int)
    parser.add_argument('--backlog',
                        help='Backlog for the listening socket (default: 128)',
                        default=128,
                        type=int)
    parser.add_argument('--loglevel',
                        help='Log level (default: info)',
                        default='info',
                        type=str,
                        choices=[
                            'critical', 'fatal', 'error',
                            'warning', 'info', 'debug',
                        ])

    return parser.parse_args()


def main():
    args = _parse_arguments()

    logging.basicConfig(level=args.loglevel.upper())

    loop = asyncio.get_event_loop()

    def root_factory(req):
        return StaticRootResource(args.root)

    req_cb = HttpRequestCB(root_factory)
    conn_cb = HttpConnectionCB(req_cb)

    starter = asyncio.start_server(conn_cb, args.bind, args.port,
                                   backlog=args.backlog,
                                   reuse_address=True,
                                   loop=loop)
    server = loop.run_until_complete(starter)

    if args.bind == '':
        logger().info('Server serving at <all interfaces>:{}'.format(args.port))
    else:
        logger().info('Server serving at {}:{}'.format(args.bind, args.port))

    try:
        loop.run_forever()
    except KeyboardInterrupt:
        pass

    server.close()
    loop.run_until_complete(server.wait_closed())
    loop.close()


if __name__ == '__main__':
    main()
