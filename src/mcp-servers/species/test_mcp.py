# test_mcp.py — run this from your Mac, not inside Docker
import asyncio
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

async def test():
    async with streamablehttp_client("http://localhost:8001/mcp") as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            tools = await session.list_tools()
            print(f"✅ Connected. Tools found: {len(tools.tools)}")
            for t in tools.tools:
                print(f"  - {t.name}")

asyncio.run(test())