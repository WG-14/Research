# Non-official Compose reference

`compose.yaml` is retained only as a portability and container-isolation
reference. It is not the supported production deployment, has not been
executed on the qualified host, and must not be used as evidence that an image,
restart, backup, restore, or TLS contract passed.

The sole official deployment is `deploy/native`: systemd supervises the web,
operations API, two outbox workers, the admitted research worker, validator,
backup, and retention audit; the host packages own PostgreSQL and Nginx. A
future switch to containers requires an explicit architecture decision,
immutable-image acceptance on the selected host, and replacement of the
`OFFICIAL_DEPLOYMENT` marker in the same reviewed release.
