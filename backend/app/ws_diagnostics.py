"""Human-readable WebSocket failure descriptions (no secrets; safe to log and expose in health)."""
from typing import Any


def describe_ws_failure(error: BaseException, close_code: int | None = None) -> str:
    """Explain a disconnect so an operator can tell a dead network path from a server close or a keepalive failure.

    A 1006 "no close frame received or sent" means the TCP connection vanished without a WebSocket
    close handshake (network drop, NAT/idle timeout, proxy, or the peer process dying). It is not,
    by itself, a message from the provider and is never caused by a clean application shutdown.
    """
    text = str(error)
    rcvd: Any = getattr(error, "rcvd", None)
    code = close_code if close_code is not None else getattr(rcvd, "code", None)
    if "keepalive ping timeout" in text or "ping timeout" in text:
        return "keepalive timeout: the peer stopped answering WebSocket pings (dead connection detected)"
    if code == 1006 or "no close frame received or sent" in text or (code is None and rcvd is None and "no close frame" in text):
        return ("abnormal closure (1006): connection dropped without a close frame - network path, "
                "NAT/idle timeout, proxy, or the upstream process restarting")
    if code in (1001, 1012, 1013):
        return f"server closed the connection ({code}): upstream is restarting or asked the client to retry later"
    if code is not None and code >= 4000:
        return f"provider rejected or closed the session (code {code})"
    if code == 1000 or type(error).__name__ == "ConnectionClosedOK":
        return "server closed the connection normally (1000)"
    if isinstance(error, TimeoutError):
        return "timed out waiting for the upstream connection"
    return f"{type(error).__name__}: {text}"[:300]
