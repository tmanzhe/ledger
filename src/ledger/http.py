"""Local JSON HTTP interface, backed by the same transactional ledger."""

from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from urllib.parse import unquote, urlsplit

import psycopg

from ledger.checker import check_invariants
from ledger.core import AccountNotFound, Ledger, LedgerError

MAX_BODY_BYTES = 16_384


class RequestError(Exception):
    def __init__(self, status: int, message: str, *, allow: str | None = None):
        self.status = status
        self.allow = allow
        super().__init__(message)


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _constant(value):
    raise ValueError("non-finite JSON number")


class LedgerHTTPServer(ThreadingHTTPServer):
    request_queue_size = 128

    def __init__(self, address: tuple[str, int], dsn: str):
        self.ledger = Ledger(dsn)
        super().__init__(address, LedgerHandler)

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(10)
        return connection, address


class LedgerHandler(BaseHTTPRequestHandler):
    server: LedgerHTTPServer
    server_version = "ledger"
    sys_version = ""

    def log_message(self, format, *args):
        # Requests can contain account IDs and keys; don't log them by default.
        pass

    def _json(self, status: int, payload: dict, *, allow: str | None = None):
        body = json.dumps(payload, ensure_ascii=True, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        if allow is not None:
            self.send_header("Allow", allow)
        self.end_headers()
        self.close_connection = True
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_error(self, code, message=None, explain=None):
        self._json(code, {"error": "HTTPError", "message": message or "invalid HTTP request"})

    def _body(self, required: set[str], optional: set[str] = frozenset()):
        if self.headers.get("Transfer-Encoding") is not None:
            raise RequestError(400, "Transfer-Encoding is not supported; use Content-Length")
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
            raise RequestError(400, "one nonnegative Content-Length is required")
        # Bound the digit count before conversion (including excessive leading zeros).
        if len(lengths[0]) > 8 or int(lengths[0]) > MAX_BODY_BYTES:
            raise RequestError(413, "JSON body exceeds 16384 bytes")
        length = int(lengths[0])
        if self.headers.get_content_type() != "application/json":
            raise RequestError(415, "Content-Type must be application/json")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise RequestError(400, "incomplete request body")
        try:
            body = json.loads(raw.decode("utf-8"), object_pairs_hook=_object,
                              parse_constant=_constant)
        except (UnicodeError, ValueError, RecursionError):
            raise RequestError(400, "body must be valid UTF-8 JSON with unique fields") from None
        if not isinstance(body, dict) or required - body.keys() or body.keys() - required - optional:
            raise RequestError(400, "JSON object has missing or unknown fields")
        for value in body.values():
            if isinstance(value, str):
                # PostgreSQL text rejects NUL; UTF-8 rejects unpaired surrogates.
                try:
                    value.encode("utf-8")
                except UnicodeError:
                    raise RequestError(400, "strings must be valid UTF-8") from None
                if "\x00" in value:
                    raise RequestError(400, "strings cannot contain NUL")
        return body

    def _dispatch(self):
        path = urlsplit(self.path).path
        if path == "/accounts":
            self._method("POST")
            body = self._body({"id"}, {"allow_negative"})
            return 200, asdict(self.server.ledger.create_account(
                body["id"], allow_negative=body.get("allow_negative", False)
            ))
        if path.startswith("/accounts/"):
            self._method("GET")
            account_id = unquote(path[len("/accounts/"):], errors="strict")
            if "\x00" in account_id:
                raise RequestError(400, "strings cannot contain NUL")
            return 200, asdict(self.server.ledger.get_account(account_id))
        if path == "/transfers":
            self._method("POST")
            body = self._body({"source_id", "destination_id", "amount_cents", "idempotency_key"})
            return 200, asdict(self.server.ledger.transfer(**body))
        if path == "/check":
            self._method("GET")
            report = check_invariants(self.server.ledger.dsn)
            return (200 if report.ok else 409), report.to_dict()
        raise RequestError(404, "route does not exist")

    def _method(self, expected: str):
        if self.command != expected:
            raise RequestError(405, f"use {expected} for this route", allow=expected)

    def _handle(self):
        allow = None
        try:
            status, body = self._dispatch()
        except RequestError as exc:
            allow = exc.allow
            status, body = exc.status, {"error": "InvalidRequest", "message": str(exc)}
        except ValueError as exc:
            status, body = 400, {"error": "InvalidRequest", "message": str(exc)}
        except AccountNotFound as exc:
            status, body = 404, {"error": type(exc).__name__, "message": str(exc)}
        except LedgerError as exc:
            status, body = 409, {"error": type(exc).__name__, "message": str(exc)}
        except psycopg.Error:
            # Driver messages may include credentials, SQL, or connection details.
            status, body = 503, {"error": "DatabaseError", "message": "database operation failed"}
        except TimeoutError:
            status, body = 408, {"error": "RequestTimeout", "message": "request body timed out"}
        self._json(status, body, allow=allow)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _handle
