"""Publish one fixed TCP service across the two Docker networks."""
from __future__ import annotations

import asyncio


async def forward(reader, writer):
    upstream = None
    try:
        remote, upstream = await asyncio.open_connection("server", 8080)

        async def copy(source, destination):
            while chunk := await source.read(65536):
                destination.write(chunk)
                await destination.drain()

        tasks = [asyncio.create_task(copy(reader, upstream)),
                 asyncio.create_task(copy(remote, writer))]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    except (OSError, asyncio.IncompleteReadError):
        pass
    finally:
        for stream in (writer, upstream):
            if stream is not None:
                stream.close()
                await stream.wait_closed()


async def main():
    server = await asyncio.start_server(forward, "0.0.0.0", 8080)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
