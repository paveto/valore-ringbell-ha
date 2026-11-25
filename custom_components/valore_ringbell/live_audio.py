"""Browser playback for the PCM samples decoded from the doorbell."""

import asyncio

from aiohttp import web


class LiveAudio:
    def __init__(self):
        self.listeners = set()
        self.chunks = 0
        self.sample_rate = None

    def publish(self, sample_rate, pcm):
        self.chunks += 1
        self.sample_rate = sample_rate
        for queue in self.listeners:
            if queue.full():
                queue.get_nowait()
            queue.put_nowait((sample_rate, pcm))

    def clear(self):
        for queue in self.listeners:
            while not queue.empty():
                queue.get_nowait()

    def close(self):
        self.clear()
        for queue in self.listeners:
            queue.put_nowait(None)

    async def websocket(self, request):
        queue = asyncio.Queue(maxsize=8)
        self.listeners.add(queue)
        socket = web.WebSocketResponse(heartbeat=20, max_msg_size=1024)

        async def send_audio():
            current_rate = None
            try:
                while (chunk := await queue.get()) is not None:
                    rate, pcm = chunk
                    if current_rate != rate:
                        await socket.send_json({"sample_rate": rate})
                        current_rate = rate
                    await socket.send_bytes(pcm)
            except ConnectionError:
                pass
            finally:
                await socket.close()

        try:
            await socket.prepare(request)
            sender = asyncio.create_task(send_audio())
            try:
                async for _ in socket:
                    pass
            finally:
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)
        finally:
            self.listeners.remove(queue)
        return socket
