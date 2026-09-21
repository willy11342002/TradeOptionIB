# NiceGUI app(main.py) 的開發用 image：只裝相依套件，原始碼不 COPY 進
# 來，由 docker-compose.yml 把整個專案目錄 mount 到 /app，改 .py 檔
# uvicorn 的 reload 就會生效，不用重 build image。相依套件(pyproject.toml/
# uv.lock)有變動才需要 `docker compose build app`。
FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# venv 刻意放在 /app 外面：/app 會被本機專案目錄整個 mount 蓋掉，本機
# 的 .venv(macOS/Windows 編出來的)也在裡面，放 /app/.venv 會被蓋掉或誤
# 用到不能跑的那份。
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

WORKDIR /app

# 只複製相依定義檔，這一層在 pyproject.toml/uv.lock 沒動的情況下會走快
# 取，改程式碼不會觸發重新安裝套件。
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-install-project --no-dev

EXPOSE 8150
CMD ["python", "main.py", "--reload", "--port", "8150"]
