FROM python:3.11-slim

WORKDIR /app

# نصب وابستگی‌ها (لایه‌ی جدا برای کش بهتر)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# کپی همه‌ی فایل‌های ماژولار:
# main.py، engine.py، base.py، nobitex.py، ompfinex.py، wallex.py، bitpin.py
COPY . .

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
