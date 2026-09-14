FROM python:3.13-slim

WORKDIR /app

# 先拷依赖文件装包(层缓存:代码变动不重装依赖)
COPY pyproject.toml README.md ./
COPY app ./app
COPY static ./static
COPY alembic ./alembic
COPY alembic.ini ./

RUN pip install --no-cache-dir .

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
