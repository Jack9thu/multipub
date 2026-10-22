import os

if os.environ.get("MULTIPUB_SIM") == "selfcheck":
    from tests.sim._routing import install

    async def handler(route, request):
        if request.url.startswith("https://sim.example/"):
            await route.fulfill(status=200, content_type="text/html", body="<html><title>ok</title><p id=x>hello</p></html>")
            return True
        return False

    install(handler)
