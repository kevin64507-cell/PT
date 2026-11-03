FROM python:3.12-slim
WORKDIR /app
COPY bot.py .
CMD ["python", "-u", "bot.py"]
