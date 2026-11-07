FROM ubuntu/squid

# The base image starts Squid by default; disable that so the project runtime
# commands can start the Python SCADA/engine application instead.
ENTRYPOINT []

WORKDIR /app

RUN apt-get update && \
	DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
		build-essential gcc make python3 python3-pip python3-venv python-is-python3 \
		openssh-server iptables && \
	(id ubuntu >/dev/null 2>&1 || useradd --create-home --shell /bin/bash ubuntu) && \
	usermod --shell /bin/bash --home /home/ubuntu ubuntu && \
	mkdir -p /home/ubuntu && chown ubuntu:ubuntu /home/ubuntu && \
	echo 'ubuntu:ubuntu' | chpasswd && \
	mkdir -p /etc/ssh/sshd_config.d && \
	printf '%s\n' 'PasswordAuthentication yes' 'KbdInteractiveAuthentication no' > /etc/ssh/sshd_config.d/99-power-attacker.conf && \
	rm -rf /var/lib/apt/lists/*

COPY . /app/power_attacker
COPY pip.conf /etc/pip.conf
ENV PIP_CONFIG_FILE=/etc/pip.conf
RUN set -eux; \
	python3 -m pip install --break-system-packages --no-cache-dir numpy scipy pytest; \
	cd /app/power_attacker/lib60870-2.4.1/lib60870-C; \
	(make clean >/dev/null 2>&1 || true); \
	make -j2 lib; \
	cd /app/power_attacker/real_iec104; \
	(make clean >/dev/null 2>&1 || true); \
	make -j2; \
	chmod +x /app/power_attacker/real_iec104/scada_server /app/power_attacker/real_iec104/scada_client; \
	chmod 700 /app
ENV PYTHONPATH=/app/power_attacker
WORKDIR /workspace
CMD ["python3", "-m", "scada_core.app", "--help"]
