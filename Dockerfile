FROM python:3.10-slim

WORKDIR /app

COPY . .

RUN pip install --upgrade pip setuptools

RUN pip install -r requirements.txt

CMD ["python", "bot.py"]
