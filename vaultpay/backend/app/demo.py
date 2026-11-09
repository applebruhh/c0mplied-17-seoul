"""Start the local demo on a fixed loopback socket without proxy header handling."""
import uvicorn

from app.config import Settings
from app.local_ops import HOST, PORT
from app.main import create_app


def main():
    settings = Settings.from_env()
    if not settings.demo_local_ops:
        raise SystemExit("Set DEMO_LOCAL_OPS=true to start the dedicated local demo listener")
    uvicorn.run(create_app(settings=settings, local_demo_listener=True),
                host=HOST, port=PORT, proxy_headers=False)


if __name__ == "__main__":
    main()
