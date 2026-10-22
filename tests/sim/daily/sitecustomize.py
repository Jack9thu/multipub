import os

if os.environ.get("MULTIPUB_SIM") == "daily":
    from tests.sim.daily.simplat import install

    install()
