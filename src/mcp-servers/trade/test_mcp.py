import asyncio, json
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

async def test():
    async with streamablehttp_client("http://localhost:8002/mcp") as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            tools = await session.list_tools()
            print("Tools:", [t.name for t in tools.tools])

            # Test coded language detector (no external API, instant)
            result = await session.call_tool("detect_coded_language", {
                "text": "Selling pangolin scales, freshly imported. DM for price. Discreet shipping."
            })
            print("Coded language:", result.content[0].text)

asyncio.run(test())