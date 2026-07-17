import os

bind = "unix:/run/research-operations-ops-api/ops-api.sock"
umask = 0o117
workers = int(os.environ.get("OPS_API_WORKERS", "2"))
threads = 1
worker_class = "sync"
timeout = 10
graceful_timeout = 15
keepalive = 2
limit_request_line = 512
limit_request_fields = 20
limit_request_field_size = 2048
max_requests = 5000
max_requests_jitter = 200
preload_app = False
worker_tmp_dir = "/run/research-operations-ops-api"
accesslog = None
errorlog = "-"
loglevel = "warning"
capture_output = False
