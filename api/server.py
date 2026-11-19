"""Yerli HTTP server (standart `wsgiref`). Defolt yalnız loopback: açıq interfeysə yalnız TLS sertifikatı ilə.
Pul/hesab/nəşr əməliyyatı yoxdur. Əmrlər: `python -m api.server serve|pair`."""
from __future__ import annotations

import argparse
import ipaddress
import ssl
from wsgiref.simple_server import WSGIRequestHandler, make_server

from api.bootstrap import Services, build_app
from api.http import wsgi_adapter

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_bind(host: str, tls_cert: str | None = None, tls_key: str | None = None) -> None:
    if bool(tls_cert) != bool(tls_key):
        raise ValueError("TLS üçün həm sertifikat, həm açar faylı verilməlidir")
    if not _is_loopback(host) and not (tls_cert and tls_key):
        raise ValueError("loopback olmayan ünvana yalnız TLS ilə bağlanmaq olar (düz HTTP açıq interfeysdə qadağandır)")


class _QuietHandler(WSGIRequestHandler):
    timeout = 15  # donmuş/yavaş müştəri tək-axınlı serveri uzun müddət bloklamasın

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 — sorğu sətri (token ola bilər) loqlanmır
        return


def create_server(svc: Services, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, tls_cert: str | None = None,
                  tls_key: str | None = None, request_timeout: float = 15):
    """Serveri qurur (başlatmır). Açıq interfeys yalnız TLS ilə; sorğu vaxt aşımı məcburidir."""
    check_bind(host, tls_cert, tls_key)
    handler = type("Handler", (_QuietHandler,), {"timeout": request_timeout})
    httpd = make_server(host, port, wsgi_adapter(svc.app), handler_class=handler)
    if tls_cert and tls_key:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(tls_cert, tls_key)
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    return httpd


def serve(data_dir: str, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, tls_cert: str | None = None,
          tls_key: str | None = None) -> None:
    check_bind(host, tls_cert, tls_key)
    httpd = create_server(build_app(data_dir, https=bool(tls_cert)), host, port, tls_cert, tls_key)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m api.server")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="API serverini başlat")
    s.add_argument("--data-dir", default="data/api")
    s.add_argument("--host", default=DEFAULT_HOST)
    s.add_argument("--port", type=int, default=DEFAULT_PORT)
    s.add_argument("--tls-cert")
    s.add_argument("--tls-key")
    pr = sub.add_parser("pair", help="Birdəfəlik cütləşmə kodu yarat (yalnız yerli konsolda)")
    pr.add_argument("--data-dir", default="data/api")
    a = ap.parse_args(argv)
    if a.cmd == "pair":
        svc = build_app(a.data_dir)
        print(svc.auth.create_pairing_code())
        return 0
    serve(a.data_dir, a.host, a.port, a.tls_cert, a.tls_key)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
