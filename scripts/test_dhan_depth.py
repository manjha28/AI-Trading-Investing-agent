#!/usr/bin/env python3
"""Probe one read-only Dhan 20-level depth subscription."""

from __future__ import annotations

import asyncio
import json
import os
import struct
from datetime import datetime, timezone
from urllib.parse import urlencode

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus, WebSocketException


RUN_SECONDS = 30
SECURITY_ID = "3045"
EXCHANGE_SEGMENT = "NSE_EQ"


def _safe_text(value: object, *secrets: str) -> str:
    text = str(value or "")
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return "".join(char if char.isprintable() else " " for char in text)[:200]


def _parse_depth_packet(packet: bytes) -> tuple[str, list[dict[str, object]]]:
    if len(packet) != 332 or int.from_bytes(packet[:2], "little") != len(packet):
        raise ValueError("unexpected depth packet length")
    if packet[3] != 1 or int.from_bytes(packet[4:8], "little", signed=True) != int(SECURITY_ID):
        raise ValueError("depth packet does not match SBIN subscription")
    if packet[2] not in {41, 51}:
        raise ValueError(f"unexpected depth response code {packet[2]}")
    levels = []
    for index in range(20):
        offset = 12 + index * 16
        levels.append({
            "price": struct.unpack_from("<d", packet, offset)[0],
            "quantity": struct.unpack_from("<I", packet, offset + 8)[0],
            "orders": struct.unpack_from("<I", packet, offset + 12)[0],
        })
    return ("bids" if packet[2] == 41 else "asks"), levels


async def run() -> int:
    access_token = os.environ.get("DHAN_ACCESS_TOKEN", "").strip()
    client_id = os.environ.get("DHAN_CLIENT_ID", "").strip()
    if not access_token or not client_id:
        print(
            "Configuration error: DHAN_ACCESS_TOKEN and DHAN_CLIENT_ID "
            "are required; no depth connection was attempted."
        )
        return 2

    url = "wss://depth-api-feed.dhan.co/twentydepth?" + urlencode({
        "token": access_token,
        "clientId": client_id,
        "authType": "2",
    })
    safe_url = (
        "wss://depth-api-feed.dhan.co/twentydepth?token=[REDACTED]"
        "&clientId=[REDACTED]&authType=2"
    )
    print(f"Connect attempt: {safe_url}")
    connection = None
    sides: dict[str, list[dict[str, object]]] = {}
    deadline = asyncio.get_running_loop().time() + RUN_SECONDS
    try:
        async with connect(
            url,
            open_timeout=10,
            ping_interval=10,
            ping_timeout=40,
            close_timeout=5,
        ) as connection:
            response = connection.response
            print(
                "Handshake result: accepted; "
                f"HTTP upgrade status: {response.status_code}; "
                f"reason: {_safe_text(response.reason_phrase, access_token, client_id)}"
            )
            await connection.send(json.dumps({
                "RequestCode": 23,
                "InstrumentCount": 1,
                "InstrumentList": [{
                    "ExchangeSegment": EXCHANGE_SEGMENT,
                    "SecurityId": SECURITY_ID,
                }],
            }))
            print("Subscription sent: RequestCode=23 NSE_EQ security id 3045")
            while asyncio.get_running_loop().time() < deadline:
                timeout = max(0, deadline - asyncio.get_running_loop().time())
                try:
                    async with asyncio.timeout(timeout):
                        message = await connection.recv()
                except TimeoutError:
                    break
                if not isinstance(message, bytes):
                    print(
                        "Received non-binary message: "
                        f"{_safe_text(message, access_token, client_id)}"
                    )
                    continue
                offset = 0
                while offset < len(message):
                    if offset + 2 > len(message):
                        raise ValueError("truncated depth packet length")
                    packet_length = int.from_bytes(message[offset:offset + 2], "little")
                    packet_end = offset + packet_length
                    if packet_length < 12 or packet_end > len(message):
                        raise ValueError("invalid depth packet boundary")
                    side, levels = _parse_depth_packet(message[offset:packet_end])
                    sides[side] = levels
                    offset = packet_end
                    if "bids" in sides and "asks" in sides:
                        print(
                            "First parsed snapshot: "
                            f"timestamp={datetime.now(timezone.utc).isoformat()} "
                            f"bids={sides['bids']} asks={sides['asks']}"
                        )
                        return 0
            print(f"No complete depth snapshot received within {RUN_SECONDS} seconds.")
    except InvalidStatus as error:
        print(
            "Handshake result: rejected; "
            f"HTTP upgrade status: {error.response.status_code}; "
            f"reason: {_safe_text(error.response.reason_phrase, access_token, client_id)}"
        )
        return 1
    except ConnectionClosed as error:
        print(
            "Connection closed by peer; "
            f"close code: {error.code}; "
            f"reason: {_safe_text(error.reason, access_token, client_id) or '(none)'}"
        )
        return 1
    except (OSError, TimeoutError, WebSocketException, ValueError, struct.error) as error:
        print(
            f"Probe failed: {type(error).__name__}; "
            f"reason: {_safe_text(error, access_token, client_id)}"
        )
        return 1
    finally:
        if connection is not None:
            print(
                f"Raw close code: {connection.close_code}; "
                "raw close reason: "
                f"{_safe_text(connection.close_reason, access_token, client_id) or '(none)'}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
