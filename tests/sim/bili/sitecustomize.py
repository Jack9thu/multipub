import os

if os.environ.get("MULTIPUB_SIM") == "bili":
    from tests.sim.bili.server import install

    install()
