FROM python:3.13.9-slim-bookworm@sha256:b685a4fa58bb19d1814d78a1ec0f0208f351452724f78b20212c984d6e124a34
WORKDIR /bot
ENV PYTHONDONTWRITEBYTECODE=1
COPY mechfight.py arena.py combat.py tactics.py bot.py ./
USER 65534:65534
ENTRYPOINT ["python3", "-u", "/bot/bot.py"]
