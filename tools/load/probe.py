"""Two HTTP responses on one TLS socket, with no reconnect or retry."""
from __future__ import annotations

import argparse
import http.client
import json
from pathlib import Path
import socket
import ssl
import time
from urllib.parse import urlparse


def probe(base, certificate, keepalive_s, *, idle_s=6):
    from server.voice.cascade import GATEWAY_KEEPALIVE_S
    if keepalive_s <= GATEWAY_KEEPALIVE_S:
        raise ValueError('fake keep-alive must exceed server GATEWAY_KEEPALIVE_S')
    url = urlparse(base)
    context = ssl.create_default_context(cafile=str(certificate))
    with socket.create_connection((url.hostname, url.port or 443), timeout=10) as raw:
        with context.wrap_socket(raw, server_hostname=url.hostname) as stream:
            for index in range(2):
                stream.sendall(f'GET /v1/models HTTP/1.1\r\nHost: {url.netloc}\r\nConnection: keep-alive\r\n\r\n'.encode('ascii'))
                response = http.client.HTTPResponse(stream)
                response.begin()
                body = response.read()
                if response.status != 200 or response.will_close or not json.loads(body).get('data'):
                    raise RuntimeError('fake keep-alive probe response was invalid or closed')
                response.close()
                if index == 0: time.sleep(idle_s)
    return {'passed': True, 'same_socket': True, 'responses': 2, 'idle_s': idle_s,
            'fake_keepalive_s': keepalive_s, 'server_pool_expiry_s': GATEWAY_KEEPALIVE_S}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding='utf-8'))
    print(json.dumps(probe('https://localhost:8443', '/load/cert.pem', config['http_keepalive_s'])))
