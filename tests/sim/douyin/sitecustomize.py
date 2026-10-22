import os

if os.environ.get("MULTIPUB_SIM") == "douyin":
    from tests.sim.douyin.mock import install

    install()
