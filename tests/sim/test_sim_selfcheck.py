"""模拟框架自检：路由生效、真实网络被拒。"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

from ._harness import REPO, find_chromium, requires_chromium

SCRIPT = r'''
import asyncio
from patchright.async_api import async_playwright
async def main():
    async with async_playwright() as pw:
        b = await pw.chromium.launch(executable_path=CHROMIUM, headless=True)
        ctx = await b.new_context()
        page = await ctx.new_page()
        await page.goto("https://sim.example/")
        print("SIM", await page.text_content("#x"))
        try:
            await page.goto("https://www.douyin.com/", timeout=5000)
            print("REAL loaded")
        except Exception as e:
            print("REAL blocked")
        await b.close()
asyncio.run(main())
'''


@requires_chromium
def test_routing_serves_mock_and_blocks_real_network(tmp_path: Path):
    script = tmp_path / "s.py"
    script.write_text(f"CHROMIUM = {find_chromium()!r}\n" + SCRIPT, encoding="utf-8")
    env = dict(os.environ, MULTIPUB_SIM="selfcheck",
               PYTHONPATH=os.pathsep.join([str(Path(__file__).parent / "selfcheck"), str(REPO)]))
    out = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, env=env, timeout=120).stdout
    assert "SIM hello" in out and "REAL blocked" in out
