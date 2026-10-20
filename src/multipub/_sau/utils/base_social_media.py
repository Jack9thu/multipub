from pathlib import Path

# multipub patch: stealth.min.js 随包分发，从本目录读取，而不是 BASE_DIR/utils/stealth.min.js；
# 同时去掉了用不到的平台常量与 get_supported_social_media / get_cli_action。
STEALTH_JS_PATH = Path(__file__).resolve().parent / "stealth.min.js"


async def set_init_script(context):
    await context.add_init_script(path=STEALTH_JS_PATH)
    return context
