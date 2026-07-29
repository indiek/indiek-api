To launch the server locally:

uv run uvicorn main:app --reload
mitmweb --mode reverse:http://127.0.0.1:8000 -p 8080
