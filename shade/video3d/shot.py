import sys, asyncio, subprocess, time
from playwright.async_api import async_playwright
async def main(frames, outdir):
    async with async_playwright() as p:
        b = await p.chromium.launch(args=['--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist', '--enable-webgl'])
        pg = await b.new_page(viewport={'width': 1920, 'height': 1080}); pg.on('console', lambda m: print('console:', m.text)); pg.on('pageerror', lambda e: print('pageerror:', e))
        await pg.goto('http://localhost:8765/index.html'); await pg.wait_for_function('window.READY===true', timeout=120000); tot = await pg.evaluate('window.TOTAL'); print('total frames', tot)
        for f in (frames if frames else range(tot)):
            t = time.time(); await pg.evaluate(f'render({f})'); await pg.screenshot(path=f'{outdir}/f{f:04d}.png'); print(f, round(time.time() - t, 1), 's', flush=True)
        await b.close()
fr = [int(x) for x in sys.argv[2].split(',')] if len(sys.argv) > 2 and sys.argv[2] != 'all' else None
asyncio.run(main(fr, sys.argv[1]))
