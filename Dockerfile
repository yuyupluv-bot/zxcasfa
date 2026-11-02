FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=3000

WORKDIR /app

RUN groupadd --system app && useradd --system --gid app --home-dir /app app

COPY requirements.txt ./
RUN python -m pip install --upgrade pip && python -m pip install -r requirements.txt

COPY --chown=app:app . .
RUN mkdir -p /app/logs /app/data/push_sounds && chown -R app:app /app/logs /app/data

USER root
EXPOSE 3000

CMD ["python", "-c", "import os,pwd,stat; from pathlib import Path; p=Path('/app/data/push_sounds'); p.mkdir(parents=True,exist_ok=True); u=pwd.getpwnam('app'); fd=os.open(p,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW); os.fchown(fd,u.pw_uid,u.pw_gid); os.fchmod(fd,stat.S_IMODE(os.fstat(fd).st_mode)|0o700); os.close(fd); os.initgroups(u.pw_name,u.pw_gid); os.setgid(u.pw_gid); os.setuid(u.pw_uid); print('STARTUP STORAGE OK: uid='+str(os.geteuid()),flush=True); os.execvp('python',['python','bothost_main.py'])"]
