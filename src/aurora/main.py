"""Sobe a API em http://localhost:8000 (`uv run api`)."""

import uvicorn


def main() -> None:
    uvicorn.run("aurora.api:api", host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
