"""Run HabitFlow on its approved loopback address."""

import errno
import socket
import sys

from werkzeug.serving import BaseWSGIServer

from habitflow import create_app

HOST = "127.0.0.1"
PORT = 5000


class _ExclusiveLoopbackServer(BaseWSGIServer):
    """Werkzeug server that cannot share the configured port on Windows."""

    allow_reuse_address = False

    def server_bind(self):
        exclusive_option = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if exclusive_option is not None:
            self.socket.setsockopt(socket.SOL_SOCKET, exclusive_option, 1)
        try:
            super().server_bind()
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE or getattr(exc, "winerror", None) == 10048:
                raise RuntimeError(
                    f"Cannot start HabitFlow: {HOST}:{PORT} is already in use. "
                    f"Close the other application using port {PORT} and try again."
                ) from None
            raise RuntimeError(
                f"Cannot start HabitFlow on {HOST}:{PORT}: {exc}"
            ) from None


def create_server():
    """Bind the application to the approved address, without port fallback."""
    return _ExclusiveLoopbackServer(HOST, PORT, create_app())


def main() -> None:
    try:
        server = create_server()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None

    print(f"HabitFlow is running at http://{HOST}:{PORT}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
