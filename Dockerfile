FROM python:3.12-slim
LABEL org.opencontainers.image.source="https://github.com/agentic-thinking/hookbus-agentspend"
LABEL org.opencontainers.image.description="HookBus-AgentSpend: free HookBus subscriber for AI agent token tracking. MIT."
LABEL org.opencontainers.image.licenses="MIT"
LABEL com.agentic-thinking.product="HookBus-AgentSpend"
LABEL com.agentic-thinking.tier="community"
LABEL org.opencontainers.image.title="HookBus-AgentSpend"
LABEL org.opencontainers.image.version="0.2.2"
LABEL org.opencontainers.image.vendor="Agentic Thinking Limited"
LABEL org.opencontainers.image.documentation="https://github.com/agentic-thinking/hookbus-agentspend"

WORKDIR /app
COPY __init__.py /app/agentspend.py
COPY HELP.md /app/HELP.md

RUN groupadd --system --gid 10001 hookbus \
 && useradd  --system --uid 10001 --gid hookbus --home-dir /home/hookbus --create-home --shell /usr/sbin/nologin hookbus \
 && mkdir -p /root/.agentspend /root/.hookbus \
 && chown -R hookbus:hookbus /app /root/.agentspend /root/.hookbus
RUN chmod 755 /root

VOLUME /root/.agentspend
EXPOSE 8879
ENV AGENTSPEND_HOST=0.0.0.0 AGENTSPEND_PORT=8879

USER hookbus

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python3 -c "import urllib.request,sys; r=urllib.request.urlopen('http://127.0.0.1:8879/', timeout=3); sys.exit(0 if r.status in (200,401) else 1)" || exit 1

CMD ["python", "-m", "agentspend"]
