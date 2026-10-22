import os

if os.environ.get("MULTIPUB_SIM") == "zhihu":
    from tests.sim.zhihu.server import install

    install()
