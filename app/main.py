"""ASGI entrypoint.

Deliberately thin: build the app, hand it to the server. Everything worth reviewing is in
app/bootstrap.py, which is the one module that knows about concrete infrastructure.

    uvicorn app.main:app
"""

from app.bootstrap import build

app = build()
