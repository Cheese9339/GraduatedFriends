# 使用 Python 3.11 slim 版本
FROM python:3.11-slim

# 安裝必要的系統工具（避免 psycopg2 等出錯）
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# 設定工作目錄
WORKDIR /app

# 安裝 Python 依賴
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 複製專案程式碼
COPY . .

# Cloud Run 預設 PORT
ENV PORT=8080
EXPOSE 8080

# 啟動 Flask 應用（用 gunicorn）
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "app:app"]
