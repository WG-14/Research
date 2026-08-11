#!/bin/sh
set -eu

django-admin migrate --noinput
research-ops migrate
django-admin collectstatic --noinput --clear

# Static assets are public adapter code, not research evidence.  They are
# projected read-only into the dedicated Nginx namespace, whose worker is not
# a member of the research filesystem group.  Normalize this one subtree to a
# fail-closed public-read contract after collectstatic has completed.
: "${INTERNAL_WEB_STATIC_ROOT:?static root required}"
test -d "$INTERNAL_WEB_STATIC_ROOT" && test ! -L "$INTERNAL_WEB_STATIC_ROOT" \
  || exit 65
if find "$INTERNAL_WEB_STATIC_ROOT" -xdev -type l -print -quit | grep -q .; then
  exit 65
fi
if find "$INTERNAL_WEB_STATIC_ROOT" -xdev \
  ! -type d ! -type f -print -quit | grep -q .; then
  exit 65
fi
if find "$INTERNAL_WEB_STATIC_ROOT" -xdev -type f -links +1 \
  -print -quit | grep -q .; then
  exit 65
fi
find "$INTERNAL_WEB_STATIC_ROOT" -xdev -type d -exec chmod 0755 {} +
find "$INTERNAL_WEB_STATIC_ROOT" -xdev -type f -exec chmod 0644 {} +
sync -f "$INTERNAL_WEB_STATIC_ROOT"

psql --set=ON_ERROR_STOP=1 \
  --set=runtime_user="$POSTGRES_RUNTIME_USER" \
  --set=diagnostics_user="$POSTGRES_DIAGNOSTICS_USER" \
  --set=validator_user="$POSTGRES_VALIDATOR_USER" \
  --set=backup_user="$POSTGRES_BACKUP_USER" <<'SQL'
SELECT format('REVOKE ALL ON ALL TABLES IN SCHEMA public, research_ops FROM %I', role_name) FROM (VALUES (:'runtime_user'), (:'diagnostics_user'), (:'validator_user'), (:'backup_user')) AS roles(role_name) \gexec
SELECT format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA public, research_ops FROM %I', role_name) FROM (VALUES (:'runtime_user'), (:'diagnostics_user'), (:'validator_user'), (:'backup_user')) AS roles(role_name) \gexec
SELECT format('GRANT USAGE ON SCHEMA public, research_ops TO %I', role_name) FROM (VALUES (:'runtime_user'), (:'diagnostics_user'), (:'validator_user'), (:'backup_user')) AS roles(role_name) \gexec

-- Shared application processes may mutate Django state and their bounded
-- coordination tables, but can never change fences, migrations, validation
-- evidence, backup registration, or restore-drill evidence.
SELECT format('GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO %I', :'runtime_user') \gexec
SELECT format('GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO %I', :'runtime_user') \gexec
SELECT format('GRANT SELECT ON ALL TABLES IN SCHEMA research_ops TO %I', :'runtime_user') \gexec
SELECT format('GRANT INSERT, UPDATE, DELETE ON research_ops.outbox_delivery, research_ops.outbox_operator_action, research_ops.worker_heartbeat, research_ops.experiment_identity, research_ops.experiment_request, research_ops.active_experiment_claim, research_ops.research_job_result_receipt TO %I', :'runtime_user') \gexec
SELECT format('GRANT INSERT, UPDATE ON research_ops.service_alert, research_ops.service_alert_delivery TO %I', :'runtime_user') \gexec
SELECT format('GRANT INSERT ON research_ops.service_alert_event TO %I', :'runtime_user') \gexec

SELECT format('GRANT USAGE ON SCHEMA public, research_ops TO %I', :'diagnostics_user') \gexec
SELECT format('GRANT SELECT ON ALL TABLES IN SCHEMA public, research_ops TO %I', :'diagnostics_user') \gexec

SELECT format('GRANT SELECT ON public.portal_webauditevent TO %I', :'validator_user') \gexec
SELECT format('GRANT SELECT, INSERT, UPDATE ON research_ops.validation_observation TO %I', :'validator_user') \gexec

SELECT format('GRANT SELECT ON ALL TABLES IN SCHEMA public, research_ops TO %I', :'backup_user') \gexec
-- pg_dump reads sequence state even with --no-owner/--no-privileges. The
-- backup identity remains read-only for sequences; it receives no USAGE or
-- UPDATE capability.
SELECT format('GRANT SELECT ON ALL SEQUENCES IN SCHEMA public, research_ops TO %I', :'backup_user') \gexec
SELECT format('GRANT UPDATE ON research_ops.runtime_control TO %I', :'backup_user') \gexec
SELECT format('GRANT INSERT, UPDATE ON research_ops.validation_observation, research_ops.backup_set, research_ops.restore_drill TO %I', :'backup_user') \gexec
SELECT format('GRANT INSERT ON research_ops.recovery_activation_event TO %I', :'backup_user') \gexec
SQL
