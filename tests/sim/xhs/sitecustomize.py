import os

if os.environ.get("MULTIPUB_SIM") == "xhs":
    from tests.sim.xhs.server import install

    install()
