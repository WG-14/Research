import os

bind = "unix:/run/research-operations-web/web.sock"
umask = 0o117
workers = int(os.environ.get("WEB_WORKERS", "2"))
threads = int(os.environ.get("WEB_THREADS", "2"))
worker_class = "gthread"
timeout = 45
graceful_timeout = 30
keepalive = 5
limit_request_line = 4094
limit_request_fields = 50
limit_request_field_size = 8190
max_requests = 2000
max_requests_jitter = 200
preload_app = False
worker_tmp_dir = "/run/research-operations-web"
accesslog = None
errorlog = "-"
loglevel = "warning"
capture_output = False
