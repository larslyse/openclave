"""Start OpenClave with the configured network settings.

    python3 run.py           start the web app
    python3 run.py --check   show what is set up (models, search, voice) and exit
"""
import sys

from config import CONFIG
from version import VERSION


def main() -> None:
    if "--check" in sys.argv[1:]:
        import setup_check
        sys.exit(setup_check.print_report())
    import uvicorn
    print(f"OpenClave {VERSION}: http://{'localhost' if CONFIG.bind_address in ('127.0.0.1', '0.0.0.0') else CONFIG.bind_address}:{CONFIG.port}/  (data: {CONFIG.home})", flush=True)
    uvicorn.run("main:app", host=CONFIG.bind_address, port=CONFIG.port)


if __name__ == "__main__":
    main()
