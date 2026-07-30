# ChatRoomMCP - MCP coordination server (task board + room chat) for multi-machine agents.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY chatroom/ ./chatroom/
COPY hooks/ ./hooks/
COPY tests/ ./tests/

# DB lives on a mounted volume; the server binds all interfaces inside the container
# (host-side publish in compose narrows which network actually reaches it).
ENV CHATROOM_DB=/data/chatroom/chatroom.db \
    PYTHONUNBUFFERED=1

EXPOSE 8080

CMD ["uvicorn", "chatroom.server:app", "--host", "0.0.0.0", "--port", "8080"]
