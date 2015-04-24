import logging


def logger(name=""):
    if name == '':
        name = __package__
    else:
        name = '.'.join([__package__, name])

    return logging.getLogger(name)
