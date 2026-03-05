import asyncio
import aiohttp

async def main():
    try:
        resolver = aiohttp.AsyncResolver(nameservers=['8.8.8.8', '8.8.4.4'])
        res = await resolver.resolve('discord.com', 443)
        print("Success:", res)
    except Exception as e:
        print("Error:", e)

if __name__ == '__main__':
    asyncio.run(main())
