#!/usr/bin/env python3
"""Exercise one read-only Dhan ticker subscription without exposing credentials."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import ssl
import struct
from datetime import datetime, timezone

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus, WebSocketException


HOST = "api-feed.dhan.co"
SECURITY_ID = "3045"
EXCHANGE_SEGMENT = "NSE_EQ"
RUN_SECONDS = 30


def _safe_text(value: object, access_token: str, client_id: str) -> str:
    text = str(value or "")
    for secret in (access_token, client_id):
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text.replace("\r", " ").replace("\n", " ")[:200]


def _parse_packet(packet: bytes) -> dict[str, object]:
    if len(packet) < 8:
        raise ValueError(f"packet shorter than response header ({len(packet)} bytes)")
    response_code = packet[0]
    declared_length = int.from_bytes(packet[1:3], "little")
    parsed: dict[str, object] = {
        "response_code": response_code,
        "declared_length": declared_length,
        "actual_length": len(packet),
        "exchange_segment_code": packet[3],
        "security_id": int.from_bytes(packet[4:8], "little", signed=True),
    }
    if declared_length != len(packet):
        raise ValueError(f"declared length {declared_length} != packet length {len(packet)}")
    if response_code == 2:
        if len(packet) != 16:
            raise ValueError(f"ticker packet length is {len(packet)}, expected 16")
        timestamp = struct.unpack_from("<i", packet, 12)[0]
        parsed.update({
            "mode": "ticker",
            "last_traded_price": struct.unpack_from("<f", packet, 8)[0],
            "last_trade_time_utc": datetime.fromtimestamp(
                timestamp, timezone.utc
            ).isoformat(),
        })
    elif response_code == 6:
        if len(packet) != 16:
            raise ValueError(f"previous-close packet length is {len(packet)}, expected 16")
        parsed.update({
            "mode": "previous_close",
            "previous_close": struct.unpack_from("<f", packet, 8)[0],
            "open_interest": struct.unpack_from("<i", packet, 12)[0],
        })
    else:
        parsed["mode"] = "other"
    return parsed


def _failure_layer(error: BaseException) -> str:
    if isinstance(error, socket.gaierror):
        return "DNS"
    if isinstance(error, ssl.SSLError):
        return "TLS"
    if isinstance(error, (ConnectionRefusedError, ConnectionResetError, TimeoutError)):
        return "TCP"
    if isinstance(error, OSError):
        return "TCP"
    if isinstance(error, WebSocketException):
        return "HTTP upgrade"
    return "WebSocket setup"


async def run() -> int:
    access_token = os.environ.get("DHAN_ACCESS_TOKEN", "").strip()
    client_id = os.environ.get("DHAN_CLIENT_ID", "").strip()
    if not access_token or not client_id:
        print("Configuration error: set DHAN_ACCESS_TOKEN and DHAN_CLIENT_ID.")
        return 2

    url = (
        f"wss://{HOST}?version=2&token={access_token}"
        f"&clientId={client_id}&authType=2"
    )
    safe_url = (
        f"wss://{HOST}?version=2&token=[REDACTED]"
        "&clientId=[REDACTED]&authType=2"
    )
    print(f"Connect attempt: {safe_url}")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + RUN_SECONDS
    connection = None
    packet_count = 0
    window_result = f"Diagnostic window ended after {RUN_SECONDS} seconds"

    try:
        async with connect(
            url,
            open_timeout=RUN_SECONDS,
            ping_interval=10,
            ping_timeout=40,
            close_timeout=5,
        ) as connection:
            response = connection.response
            print(
                "Handshake result: accepted; "
                f"HTTP upgrade status: {response.status_code}; "
                f"reason: {_safe_text(response.reason_phrase, access_token, client_id)}; "
                f"subscription: Ticker {EXCHANGE_SEGMENT} security id {SECURITY_ID}"
            )
            await connection.send(json.dumps({
                "RequestCode": 15,
                "InstrumentCount": 1,
                "InstrumentList": [{
                    "ExchangeSegment": EXCHANGE_SEGMENT,
                    "SecurityId": SECURITY_ID,
                }],
            }))

            try:
                async with asyncio.timeout(max(0, deadline - loop.time())):
                    while True:
                        message = await connection.recv()
                        if packet_count >= 5:
                            continue
                        packet_count += 1
                        if not isinstance(message, bytes):
                            print(
                                f"Packet {packet_count}: raw text "
                                f"{_safe_text(message, access_token, client_id)}; "
                                "parse error: expected binary packet"
                            )
                            continue
                        try:
                            parsed = _parse_packet(message)
                        except (OverflowError, OSError, ValueError, struct.error) as error:
                            print(
                                f"Packet {packet_count}: raw_hex={message.hex()}; "
                                "parse error: "
                                f"{_safe_text(error, access_token, client_id)}"
                            )
                        else:
                            print(
                                f"Packet {packet_count}: raw_hex={message.hex()}; "
                                f"parsed={parsed}"
                            )
            except TimeoutError:
                if packet_count == 0:
                    window_result = f"No packets received within {RUN_SECONDS} seconds"
    except InvalidStatus as error:
        response = error.response
        status = response.status_code
        layer = "auth" if status in {401, 403} else "HTTP upgrade"
        print(
            f"Handshake result: rejected; HTTP upgrade status: {status}; "
            f"reason: {_safe_text(response.reason_phrase, access_token, client_id)}; "
            f"failed layer: {layer}"
        )
        return 1
    except ConnectionClosed as error:
        window_result = (
            "Connection closed by peer; "
            f"reason: {_safe_text(error.reason, access_token, client_id) or '(none)'}"
        )
        return 1
    except (OSError, TimeoutError, WebSocketException, ssl.SSLError) as error:
        print(
            f"Handshake result: failed; failed layer: {_failure_layer(error)}; "
            f"error class: {type(error).__name__}; "
            f"reason: {_safe_text(error, access_token, client_id)}"
        )
        return 1
    finally:
        if connection is not None:
            close_code = connection.close_code
            close_reason = _safe_text(
                connection.close_reason, access_token, client_id
            )
            print(
                f"{window_result}; close code: {close_code}; "
                f"reason: {close_reason or '(none)'}"
            )

    return 0


if __name__ == "__main__":
    exit_code = asyncio.run(run())
    print(f"Script exit code: {exit_code}")
    raise SystemExit(exit_code)
