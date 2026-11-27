from __future__ import annotations

from fastapi import APIRouter, WebSocket

from app.ws.connection import handle_transcribe

router = APIRouter()


@router.websocket("/ws/transcribe")
async def transcribe(websocket: WebSocket) -> None:
    """Live transcription. Protocol: docs/websocket-protocol.md."""
    await handle_transcribe(websocket)
