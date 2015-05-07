###########
What's Pyx?
###########

Pyx is yet another async web server written in Python, with the ``asyncio``
module.

It's small and simple. It has no dependency at all except Python itself.

Yet Pyx is also carefully crafted to behave nicely in stressful and
dangerous enviroments.

###############
How to install?
###############

Just use pip or tools alike:

.. code-block:: sh

    pip install pyxserver

################
What can Pyx do?
################

Pyx is a simple static file server by itself:

.. code-block:: sh

    pyx -b localhost -p 8000 -r /some/where

This will start pyx and bind it to localhost:8000, serving files in the
directory /some/where

And you can also use the small framework provided by Pyx to write your
own dynamic web application:

.. code-block:: python

    import asyncio
    from pyx import (HttpHeader, HttpConnectionCB)

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

Please see the implementation of ``pyx.http.StaticRootResource`` for a
more sophisticated example.

#######
License
#######

Pyx is licensed under the terms of `the MIT license`_.

.. _the MIT license: http://l04m33.mit-license.org/
