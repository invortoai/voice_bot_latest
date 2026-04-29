import asyncio
import time
import typing
from typing import Awaitable, Callable, Optional

from loguru import logger
from pydantic import BaseModel

from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    Frame,
    InputAudioRawFrame,
    OutputAudioRawFrame,
    StartFrame,
    StartInterruptionFrame,
    TransportMessageFrame,
    TransportMessageUrgentFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.serializers.base_serializer import FrameSerializer
from pipecat.transports.base_input import BaseInputTransport
from pipecat.transports.base_output import BaseOutputTransport
from pipecat.transports.base_transport import BaseTransport, TransportParams

try:
    from fastapi import WebSocket
    from starlette.websockets import WebSocketState
except ModuleNotFoundError as e:
    logger.error(f"Exception: {e}")
    logger.error(
        "In order to use FastAPI websockets, you need to `pip install pipecat-ai[websocket]`."
    )
    raise Exception(f"Missing module: {e}")


class McubeTransportParams(TransportParams):
    """Configuration parameters for MCube WebSocket transport."""

    serializer: Optional[FrameSerializer] = None
    session_timeout: Optional[int] = None


class McubeTransportCallbacks(BaseModel):
    on_client_connected: Callable[[WebSocket], Awaitable[None]]
    on_client_disconnected: Callable[[WebSocket], Awaitable[None]]
    on_session_timeout: Callable[[WebSocket], Awaitable[None]]


class McubeTransportClient:
    """Client wrapper for MCube WebSocket connections.

    Handles both binary (audio) and text (control) messages.
    """

    def __init__(
        self,
        websocket: WebSocket,
        callbacks: McubeTransportCallbacks,
        params: McubeTransportParams,
    ):
        self._websocket = websocket
        self._callbacks = callbacks
        self._params = params
        self._closing = False
        self._leave_counter = 0

    async def setup(self, _: StartFrame):
        self._leave_counter += 1

    def receive(self) -> typing.AsyncIterator[bytes | str]:
        async def _iter():
            while True:
                msg = await self._websocket.receive()
                msg_type = msg.get("type")
                if msg_type == "websocket.disconnect":
                    break

                if msg_type != "websocket.receive":
                    continue

                data_bytes = msg.get("bytes")
                if data_bytes is not None:
                    yield data_bytes
                    continue

                data_text = msg.get("text")
                if data_text is not None:
                    yield data_text

        return _iter()

    async def send(self, data: str | bytes):
        try:
            if self._can_send():
                if isinstance(data, (bytes, bytearray)):
                    await self._websocket.send_bytes(bytes(data))
                else:
                    # Handle multiple messages separated by newline
                    # (for playAudio + checkpoint)
                    if "\n" in data:
                        messages = data.split("\n")
                        for msg in messages:
                            if msg.strip():
                                await self._websocket.send_text(msg)
                    else:
                        await self._websocket.send_text(data)
        except Exception as e:
            logger.error(
                f"{self} exception sending data: {e.__class__.__name__} ({e}), "
                f"application_state: {self._websocket.application_state}"
            )
            if (
                self._websocket.application_state == WebSocketState.DISCONNECTED
                and not self.is_closing
            ):
                logger.warning("Closing already disconnected websocket!")
                self._closing = True

    async def disconnect(self):
        self._leave_counter -= 1
        if self._leave_counter > 0:
            return

        if self.is_connected and not self.is_closing:
            self._closing = True
            try:
                await self._websocket.close()
            except Exception as e:
                logger.error(f"{self} exception while closing the websocket: {e}")

    async def trigger_client_disconnected(self):
        await self._callbacks.on_client_disconnected(self._websocket)

    async def trigger_client_connected(self):
        await self._callbacks.on_client_connected(self._websocket)

    async def trigger_client_timeout(self):
        await self._callbacks.on_session_timeout(self._websocket)

    def _can_send(self):
        return self.is_connected and not self.is_closing

    @property
    def is_connected(self) -> bool:
        return self._websocket.client_state == WebSocketState.CONNECTED

    @property
    def is_closing(self) -> bool:
        return self._closing


class McubeInputTransport(BaseInputTransport):
    """Input transport for MCube WebSocket connections."""

    def __init__(
        self,
        transport: BaseTransport,
        client: McubeTransportClient,
        params: McubeTransportParams,
        **kwargs,
    ):
        super().__init__(params, **kwargs)
        self._transport = transport
        self._client = client
        self._params = params
        self._receive_task = None
        self._monitor_websocket_task = None
        self._initialized = False

    async def start(self, frame: StartFrame):
        await super().start(frame)

        if self._initialized:
            return
        self._initialized = True

        await self._client.setup(frame)
        if self._params.serializer:
            await self._params.serializer.setup(frame)

        if not self._monitor_websocket_task and self._params.session_timeout:
            self._monitor_websocket_task = self.create_task(self._monitor_websocket())

        await self._client.trigger_client_connected()

        if not self._receive_task:
            self._receive_task = self.create_task(self._receive_messages())

        await self.set_transport_ready(frame)

    async def _stop_tasks(self):
        if self._monitor_websocket_task:
            await self.cancel_task(self._monitor_websocket_task)
            self._monitor_websocket_task = None
        if self._receive_task:
            await self.cancel_task(self._receive_task)
            self._receive_task = None

    async def stop(self, frame: EndFrame):
        await super().stop(frame)
        await self._stop_tasks()
        await self._client.disconnect()

    async def cancel(self, frame: CancelFrame):
        await super().cancel(frame)
        await self._stop_tasks()
        await self._client.disconnect()

    async def cleanup(self):
        await super().cleanup()
        await self._transport.cleanup()

    async def _receive_messages(self):
        try:
            async for message in self._client.receive():
                if not self._params.serializer:
                    continue

                frame = await self._params.serializer.deserialize(message)
                if not frame:
                    continue

                if isinstance(frame, InputAudioRawFrame):
                    await self.push_audio_frame(frame)
                else:
                    await self.push_frame(frame)
        except Exception as e:
            logger.error(
                f"{self} exception receiving data: {e.__class__.__name__} ({e})"
            )

        if not self._client.is_closing:
            await self._client.trigger_client_disconnected()

    async def _monitor_websocket(self):
        await asyncio.sleep(self._params.session_timeout)
        await self._client.trigger_client_timeout()


class McubeOutputTransport(BaseOutputTransport):
    """Output transport for MCube WebSocket connections."""

    def __init__(
        self,
        transport: BaseTransport,
        client: McubeTransportClient,
        params: McubeTransportParams,
        **kwargs,
    ):
        super().__init__(params, **kwargs)
        self._transport = transport
        self._client = client
        self._params = params

        self._send_interval = 0
        self._next_send_time = 0

        self._initialized = False

    async def start(self, frame: StartFrame):
        await super().start(frame)

        if self._initialized:
            return
        self._initialized = True

        if self._params.serializer:
            await self._params.serializer.setup(frame)

        # Simulate audio device timing
        audio_bytes_10ms = (
            int(self.sample_rate / 100) * self._params.audio_out_channels * 2
        )
        chunk_bytes = audio_bytes_10ms * self._params.audio_out_10ms_chunks
        self._send_interval = (
            chunk_bytes / (self.sample_rate * self._params.audio_out_channels * 2)
        ) or 0

        await self.set_transport_ready(frame)

    async def cleanup(self):
        await super().cleanup()
        await self._transport.cleanup()

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, StartInterruptionFrame):
            await self._write_frame(frame)
            self._next_send_time = 0

    async def send_message(
        self, frame: TransportMessageFrame | TransportMessageUrgentFrame
    ):
        await self._write_frame(frame)

    async def write_audio_frame(self, frame: OutputAudioRawFrame) -> bool:
        if self._client.is_closing or not self._client.is_connected:
            return False

        frame = OutputAudioRawFrame(
            audio=frame.audio,
            sample_rate=self.sample_rate,
            num_channels=self._params.audio_out_channels,
        )

        await self._write_frame(frame)
        await self._write_audio_sleep()
        return True

    async def _write_frame(self, frame: Frame):
        if self._client.is_closing or not self._client.is_connected:
            return

        if not self._params.serializer:
            return

        try:
            payload = await self._params.serializer.serialize(frame)
            if payload:
                await self._client.send(payload)
        except Exception as e:
            logger.error(f"{self} exception sending data: {e.__class__.__name__} ({e})")

    async def _write_audio_sleep(self):
        current_time = time.monotonic()
        sleep_duration = max(0, self._next_send_time - current_time)
        await asyncio.sleep(sleep_duration)
        if sleep_duration == 0:
            self._next_send_time = time.monotonic() + self._send_interval
        else:
            self._next_send_time += self._send_interval


class McubeTransport(BaseTransport):
    """MCube WebSocket transport for real-time audio streaming."""

    def __init__(
        self,
        websocket: WebSocket,
        params: McubeTransportParams,
        input_name: Optional[str] = None,
        output_name: Optional[str] = None,
    ):
        super().__init__(input_name=input_name, output_name=output_name)

        self._params = params

        self._callbacks = McubeTransportCallbacks(
            on_client_connected=self._on_client_connected,
            on_client_disconnected=self._on_client_disconnected,
            on_session_timeout=self._on_session_timeout,
        )

        self._client = McubeTransportClient(websocket, self._callbacks, self._params)
        self._input = McubeInputTransport(
            self, self._client, self._params, name=self._input_name
        )
        self._output = McubeOutputTransport(
            self, self._client, self._params, name=self._output_name
        )

        self._register_event_handler("on_client_connected")
        self._register_event_handler("on_client_disconnected")
        self._register_event_handler("on_session_timeout")

    def input(self) -> McubeInputTransport:
        return self._input

    def output(self) -> McubeOutputTransport:
        return self._output

    async def _on_client_connected(self, websocket):
        await self._call_event_handler("on_client_connected", websocket)

    async def _on_client_disconnected(self, websocket):
        await self._call_event_handler("on_client_disconnected", websocket)

    async def _on_session_timeout(self, websocket):
        await self._call_event_handler("on_session_timeout", websocket)
