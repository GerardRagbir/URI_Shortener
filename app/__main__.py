"""``python -m app`` starts the server on the configured host and port."""
import uvicorn

from .config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run('app.main:app', host=settings.host, port=settings.port)


if __name__ == '__main__':
    main()
