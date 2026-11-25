"""tenant session context and row security

Revision ID: 9fa01f884e8f
Revises: 76d2abb6b050
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "9fa01f884e8f"
down_revision: Union[str, Sequence[str], None] = "76d2abb6b050"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _execute(statements: list[str]) -> None:
    for statement in statements:
        op.execute(sa.text(statement))


def upgrade() -> None:
    statements = [

        # ---------------------------------------------------------
        # 1. Protected transaction authentication context
        # ---------------------------------------------------------

        """
        CREATE TABLE healthops.auth_context (
            backend_pid integer NOT NULL,
            transaction_id xid8 NOT NULL,
            user_id uuid NOT NULL,
            organization_id uuid NOT NULL,
            membership_role varchar(20) NOT NULL,
            created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT pk_auth_context
                PRIMARY KEY (backend_pid, transaction_id),
            CONSTRAINT ck_auth_context_role
                CHECK (
                    membership_role IN (
                        'owner', 'admin', 'analyst', 'viewer'
                    )
                )
        )
        """,

        """
        CREATE INDEX ix_auth_context_created_at
        ON healthops.auth_context (created_at)
        """,

        """
        REVOKE ALL ON TABLE healthops.auth_context
        FROM PUBLIC, healthops_app
        """,

        # ---------------------------------------------------------
        # 2. Read-only transaction context accessors
        # ---------------------------------------------------------

        """
        CREATE FUNCTION healthops.current_organization_id()
        RETURNS uuid
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp
        AS $function$
            SELECT c.organization_id
            FROM healthops.auth_context AS c
            WHERE c.backend_pid = pg_catalog.pg_backend_pid()
              AND c.transaction_id = pg_catalog.pg_current_xact_id()
            LIMIT 1
        $function$
        """,

        """
        CREATE FUNCTION healthops.current_user_id()
        RETURNS uuid
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp
        AS $function$
            SELECT c.user_id
            FROM healthops.auth_context AS c
            WHERE c.backend_pid = pg_catalog.pg_backend_pid()
              AND c.transaction_id = pg_catalog.pg_current_xact_id()
            LIMIT 1
        $function$
        """,

        """
        CREATE FUNCTION healthops.current_membership_role()
        RETURNS text
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp
        AS $function$
            SELECT c.membership_role::text
            FROM healthops.auth_context AS c
            WHERE c.backend_pid = pg_catalog.pg_backend_pid()
              AND c.transaction_id = pg_catalog.pg_current_xact_id()
            LIMIT 1
        $function$
        """,

        # ---------------------------------------------------------
        # 3. Resolve a valid session and its active memberships
        # ---------------------------------------------------------

        """
        CREATE FUNCTION healthops.resolve_session(p_token_hash text)
        RETURNS TABLE (
            user_id uuid,
            email text,
            display_name text,
            organization_id uuid,
            organization_name text,
            organization_slug text,
            membership_role text
        )
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp
        AS $function$
            SELECT
                u.id,
                u.email,
                u.display_name,
                active_memberships.organization_id,
                active_memberships.organization_name,
                active_memberships.organization_slug,
                active_memberships.membership_role
            FROM healthops.user_sessions AS s
            JOIN healthops.users AS u
                ON u.id = s.user_id
            LEFT JOIN LATERAL (
                SELECT
                    m.organization_id,
                    o.name AS organization_name,
                    o.slug AS organization_slug,
                    m.role::text AS membership_role
                FROM healthops.memberships AS m
                JOIN healthops.organizations AS o
                    ON o.id = m.organization_id
                WHERE m.user_id = u.id
                  AND m.status = 'active'
                  AND o.status = 'active'
                ORDER BY o.name, o.id
            ) AS active_memberships ON TRUE
            WHERE p_token_hash ~ '^[0-9a-f]{64}$'
              AND s.token_hash = p_token_hash
              AND s.revoked_at IS NULL
              AND s.expires_at > pg_catalog.clock_timestamp()
              AND u.is_active = TRUE
            ORDER BY active_memberships.organization_name NULLS LAST
        $function$
        """,

        # ---------------------------------------------------------
        # 4. Validate session + organization membership and bind
        #    the authenticated identity to this transaction.
        # ---------------------------------------------------------

        """
        CREATE FUNCTION healthops.open_tenant_context(
            p_token_hash text,
            p_organization_id uuid
        )
        RETURNS TABLE (
            authenticated_user_id uuid,
            active_membership_role text
        )
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp
        AS $function$
        DECLARE
            v_user_id uuid;
            v_role text;
            v_existing_user_id uuid;
            v_existing_organization_id uuid;
            v_backend_pid integer;
            v_transaction_id xid8;
        BEGIN
            IF p_token_hash IS NULL
               OR p_token_hash !~ '^[0-9a-f]{64}$'
               OR p_organization_id IS NULL
            THEN
                RAISE EXCEPTION 'Invalid session or organization access'
                    USING ERRCODE = '28000';
            END IF;

            v_backend_pid := pg_catalog.pg_backend_pid();
            v_transaction_id := pg_catalog.pg_current_xact_id();

            SELECT u.id, m.role::text
            INTO v_user_id, v_role
            FROM healthops.user_sessions AS s
            JOIN healthops.users AS u
                ON u.id = s.user_id
            JOIN healthops.memberships AS m
                ON m.user_id = u.id
               AND m.organization_id = p_organization_id
            JOIN healthops.organizations AS o
                ON o.id = m.organization_id
            WHERE s.token_hash = p_token_hash
              AND s.revoked_at IS NULL
              AND s.expires_at > pg_catalog.clock_timestamp()
              AND u.is_active = TRUE
              AND m.status = 'active'
              AND o.status = 'active'
            FOR SHARE OF s, u, m, o;

            IF NOT FOUND THEN
                RAISE EXCEPTION 'Invalid session or organization access'
                    USING ERRCODE = '28000';
            END IF;

            -- Clear older transaction contexts left by this pooled
            -- PostgreSQL backend connection.
            DELETE FROM healthops.auth_context AS c
            WHERE c.backend_pid = v_backend_pid
              AND c.transaction_id <> v_transaction_id;

            -- A transaction cannot switch users or organizations
            -- after its tenant context has been established.
            SELECT c.user_id, c.organization_id
            INTO v_existing_user_id, v_existing_organization_id
            FROM healthops.auth_context AS c
            WHERE c.backend_pid = v_backend_pid
              AND c.transaction_id = v_transaction_id;

            IF FOUND THEN
                IF v_existing_user_id <> v_user_id
                   OR v_existing_organization_id <> p_organization_id
                THEN
                    RAISE EXCEPTION 'Tenant context cannot be changed'
                        USING ERRCODE = '28000';
                END IF;
            ELSE
                INSERT INTO healthops.auth_context (
                    backend_pid,
                    transaction_id,
                    user_id,
                    organization_id,
                    membership_role
                )
                VALUES (
                    v_backend_pid,
                    v_transaction_id,
                    v_user_id,
                    p_organization_id,
                    v_role
                );
            END IF;

            -- Session activity timestamps will be maintained by a
            -- separate throttled operation. Do not upgrade the shared
            -- session lock here: concurrent requests could deadlock.

            RETURN QUERY
            SELECT v_user_id, v_role;
        END;
        $function$
        """,

        # ---------------------------------------------------------
        # 5. Restrict SECURITY DEFINER function execution
        # ---------------------------------------------------------

        """
        REVOKE ALL ON FUNCTION
            healthops.current_organization_id()
        FROM PUBLIC
        """,

        """
        REVOKE ALL ON FUNCTION
            healthops.current_user_id()
        FROM PUBLIC
        """,

        """
        REVOKE ALL ON FUNCTION
            healthops.current_membership_role()
        FROM PUBLIC
        """,

        """
        REVOKE ALL ON FUNCTION
            healthops.resolve_session(text)
        FROM PUBLIC
        """,

        """
        REVOKE ALL ON FUNCTION
            healthops.open_tenant_context(text, uuid)
        FROM PUBLIC
        """,

        """
        GRANT EXECUTE ON FUNCTION
            healthops.current_organization_id()
        TO healthops_app
        """,

        """
        GRANT EXECUTE ON FUNCTION
            healthops.current_user_id()
        TO healthops_app
        """,

        """
        GRANT EXECUTE ON FUNCTION
            healthops.current_membership_role()
        TO healthops_app
        """,

        """
        GRANT EXECUTE ON FUNCTION
            healthops.resolve_session(text)
        TO healthops_app
        """,

        """
        GRANT EXECUTE ON FUNCTION
            healthops.open_tenant_context(text, uuid)
        TO healthops_app
        """,

        # ---------------------------------------------------------
        # 6. Restrict direct access to authentication tables
        # ---------------------------------------------------------

        """
        REVOKE ALL ON TABLE
            healthops.organizations,
            healthops.users,
            healthops.memberships,
            healthops.user_sessions
        FROM PUBLIC, healthops_app
        """,

        """
        GRANT SELECT ON TABLE healthops.organizations
        TO healthops_app
        """,

        """
        GRANT SELECT (
            id,
            email,
            email_normalized,
            display_name,
            is_active,
            email_verified_at,
            created_at,
            updated_at
        )
        ON TABLE healthops.users
        TO healthops_app
        """,

        """
        GRANT UPDATE (display_name, updated_at)
        ON TABLE healthops.users
        TO healthops_app
        """,

        """
        GRANT SELECT ON TABLE healthops.memberships
        TO healthops_app
        """,

        # ---------------------------------------------------------
        """
        REVOKE ALL ON TABLE healthops.alembic_version
        FROM PUBLIC, healthops_app
        """,

        # Future tables must receive explicit grants, never blanket CRUD.
        """
        ALTER DEFAULT PRIVILEGES
        FOR ROLE healthops_owner
        IN SCHEMA healthops
        REVOKE ALL ON TABLES FROM healthops_app
        """,

        """
        ALTER DEFAULT PRIVILEGES
        FOR ROLE healthops_owner
        IN SCHEMA healthops
        REVOKE ALL ON SEQUENCES FROM healthops_app
        """,

        # 7. Restrict append-only tables
        # ---------------------------------------------------------

        """
        REVOKE ALL ON TABLE
            healthops.dataset_versions,
            healthops.chat_messages,
            healthops.audit_events
        FROM PUBLIC, healthops_app
        """,

        """
        GRANT SELECT, INSERT ON TABLE
            healthops.dataset_versions,
            healthops.chat_messages,
            healthops.audit_events
        TO healthops_app
        """,

        # ---------------------------------------------------------
        # Explicit privileges for tenant-owned operational tables.
        """
        REVOKE ALL ON TABLE
            healthops.workspaces,
            healthops.data_sources,
            healthops.datasets,
            healthops.analysis_jobs,
            healthops.chat_conversations,
            healthops.saved_dashboards,
            healthops.cleaning_runs
        FROM PUBLIC, healthops_app
        """,

        """
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE
            healthops.workspaces,
            healthops.data_sources,
            healthops.datasets,
            healthops.analysis_jobs,
            healthops.chat_conversations,
            healthops.saved_dashboards,
            healthops.cleaning_runs
        TO healthops_app
        """,

        # 8. RLS for global identity / authentication tables
        #
        # These tables deliberately do not FORCE RLS:
        # SECURITY DEFINER authentication functions are owned by
        # healthops_owner, which must validate them before a tenant
        # context exists. The application role is still constrained
        # by grants and the policies below.
        # ---------------------------------------------------------

        """
        ALTER TABLE healthops.organizations
        ENABLE ROW LEVEL SECURITY
        """,

        """
        CREATE POLICY organizations_read_current
        ON healthops.organizations
        FOR SELECT
        TO healthops_app
        USING (
            id = healthops.current_organization_id()
        )
        """,

        """
        ALTER TABLE healthops.users
        ENABLE ROW LEVEL SECURITY
        """,

        """
        CREATE POLICY users_read_self
        ON healthops.users
        FOR SELECT
        TO healthops_app
        USING (
            id = healthops.current_user_id()
        )
        """,

        """
        CREATE POLICY users_update_self
        ON healthops.users
        FOR UPDATE
        TO healthops_app
        USING (
            id = healthops.current_user_id()
        )
        WITH CHECK (
            id = healthops.current_user_id()
        )
        """,

        """
        ALTER TABLE healthops.memberships
        ENABLE ROW LEVEL SECURITY
        """,

        """
        CREATE POLICY memberships_read_self_or_admin
        ON healthops.memberships
        FOR SELECT
        TO healthops_app
        USING (
            organization_id = healthops.current_organization_id()
            AND (
                user_id = healthops.current_user_id()
                OR healthops.current_membership_role()
                    IN ('owner', 'admin')
            )
        )
        """,

        """
        ALTER TABLE healthops.user_sessions
        ENABLE ROW LEVEL SECURITY
        """,

        # No application policy or direct table privilege is granted
        # for user_sessions. Authentication functions access it as
        # the protected table owner.

        # ---------------------------------------------------------
        # 9. Enable and FORCE RLS on tenant-owned tables
        # ---------------------------------------------------------

        """
        ALTER TABLE healthops.workspaces
        ENABLE ROW LEVEL SECURITY
        """,
        """
        ALTER TABLE healthops.workspaces
        FORCE ROW LEVEL SECURITY
        """,

        """
        ALTER TABLE healthops.data_sources
        ENABLE ROW LEVEL SECURITY
        """,
        """
        ALTER TABLE healthops.data_sources
        FORCE ROW LEVEL SECURITY
        """,

        """
        ALTER TABLE healthops.datasets
        ENABLE ROW LEVEL SECURITY
        """,
        """
        ALTER TABLE healthops.datasets
        FORCE ROW LEVEL SECURITY
        """,

        """
        ALTER TABLE healthops.dataset_versions
        ENABLE ROW LEVEL SECURITY
        """,
        """
        ALTER TABLE healthops.dataset_versions
        FORCE ROW LEVEL SECURITY
        """,

        """
        ALTER TABLE healthops.analysis_jobs
        ENABLE ROW LEVEL SECURITY
        """,
        """
        ALTER TABLE healthops.analysis_jobs
        FORCE ROW LEVEL SECURITY
        """,

        """
        ALTER TABLE healthops.chat_conversations
        ENABLE ROW LEVEL SECURITY
        """,
        """
        ALTER TABLE healthops.chat_conversations
        FORCE ROW LEVEL SECURITY
        """,

        """
        ALTER TABLE healthops.chat_messages
        ENABLE ROW LEVEL SECURITY
        """,
        """
        ALTER TABLE healthops.chat_messages
        FORCE ROW LEVEL SECURITY
        """,

        """
        ALTER TABLE healthops.saved_dashboards
        ENABLE ROW LEVEL SECURITY
        """,
        """
        ALTER TABLE healthops.saved_dashboards
        FORCE ROW LEVEL SECURITY
        """,

        """
        ALTER TABLE healthops.cleaning_runs
        ENABLE ROW LEVEL SECURITY
        """,
        """
        ALTER TABLE healthops.cleaning_runs
        FORCE ROW LEVEL SECURITY
        """,

        """
        ALTER TABLE healthops.audit_events
        ENABLE ROW LEVEL SECURITY
        """,
        """
        ALTER TABLE healthops.audit_events
        FORCE ROW LEVEL SECURITY
        """,

        # ---------------------------------------------------------
        # 10. Workspace policies
        # ---------------------------------------------------------

        """
        CREATE POLICY workspaces_read_org
        ON healthops.workspaces
        FOR SELECT TO healthops_app
        USING (
            organization_id = healthops.current_organization_id()
        )
        """,

        """
        CREATE POLICY workspaces_write_admin
        ON healthops.workspaces
        FOR ALL TO healthops_app
        USING (
            organization_id = healthops.current_organization_id()
            AND healthops.current_membership_role()
                IN ('owner', 'admin')
        )
        WITH CHECK (
            organization_id = healthops.current_organization_id()
            AND healthops.current_membership_role()
                IN ('owner', 'admin')
        )
        """,

        # ---------------------------------------------------------
        # 11. Data source policies
        # ---------------------------------------------------------

        """
        CREATE POLICY data_sources_read_org
        ON healthops.data_sources
        FOR SELECT TO healthops_app
        USING (
            organization_id = healthops.current_organization_id()
        )
        """,

        """
        CREATE POLICY data_sources_write_analyst
        ON healthops.data_sources
        FOR ALL TO healthops_app
        USING (
            organization_id = healthops.current_organization_id()
            AND healthops.current_membership_role()
                IN ('owner', 'admin', 'analyst')
        )
        WITH CHECK (
            organization_id = healthops.current_organization_id()
            AND healthops.current_membership_role()
                IN ('owner', 'admin', 'analyst')
        )
        """,

        # ---------------------------------------------------------
        # 12. Dataset policies
        # ---------------------------------------------------------

        """
        CREATE POLICY datasets_read_org
        ON healthops.datasets
        FOR SELECT TO healthops_app
        USING (
            organization_id = healthops.current_organization_id()
        )
        """,

        """
        CREATE POLICY datasets_write_analyst
        ON healthops.datasets
        FOR ALL TO healthops_app
        USING (
            organization_id = healthops.current_organization_id()
            AND healthops.current_membership_role()
                IN ('owner', 'admin', 'analyst')
        )
        WITH CHECK (
            organization_id = healthops.current_organization_id()
            AND healthops.current_membership_role()
                IN ('owner', 'admin', 'analyst')
        )
        """,

        # ---------------------------------------------------------
        # 13. Immutable dataset version policies
        # ---------------------------------------------------------

        """
        CREATE POLICY dataset_versions_read_org
        ON healthops.dataset_versions
        FOR SELECT TO healthops_app
        USING (
            organization_id = healthops.current_organization_id()
        )
        """,

        """
        CREATE POLICY dataset_versions_insert_analyst
        ON healthops.dataset_versions
        FOR INSERT TO healthops_app
        WITH CHECK (
            organization_id = healthops.current_organization_id()
            AND healthops.current_membership_role()
                IN ('owner', 'admin', 'analyst')
        )
        """,

        # ---------------------------------------------------------
        # 14. Analysis job policies
        # ---------------------------------------------------------

        """
        CREATE POLICY analysis_jobs_read_org
        ON healthops.analysis_jobs
        FOR SELECT TO healthops_app
        USING (
            organization_id = healthops.current_organization_id()
        )
        """,

        """
        CREATE POLICY analysis_jobs_insert_org
        ON healthops.analysis_jobs
        FOR INSERT TO healthops_app
        WITH CHECK (
            organization_id = healthops.current_organization_id()
            AND requested_by_user_id = healthops.current_user_id()
            AND healthops.current_membership_role()
                IN ('owner', 'admin', 'analyst')
        )
        """,

        """
        CREATE POLICY analysis_jobs_update_org
        ON healthops.analysis_jobs
        FOR UPDATE TO healthops_app
        USING (
            organization_id = healthops.current_organization_id()
            AND healthops.current_membership_role()
                IN ('owner', 'admin', 'analyst')
        )
        WITH CHECK (
            organization_id = healthops.current_organization_id()
            AND healthops.current_membership_role()
                IN ('owner', 'admin', 'analyst')
        )
        """,

        """
        CREATE POLICY analysis_jobs_delete_admin
        ON healthops.analysis_jobs
        FOR DELETE TO healthops_app
        USING (
            organization_id = healthops.current_organization_id()
            AND healthops.current_membership_role()
                IN ('owner', 'admin')
        )
        """,

        # ---------------------------------------------------------
        # 15. Conversation policies
        # ---------------------------------------------------------

        """
        CREATE POLICY chat_conversations_read
        ON healthops.chat_conversations
        FOR SELECT TO healthops_app
        USING (
            organization_id = healthops.current_organization_id()
            AND (
                created_by_user_id = healthops.current_user_id()
                OR healthops.current_membership_role()
                    IN ('owner', 'admin')
            )
        )
        """,

        """
        CREATE POLICY chat_conversations_insert
        ON healthops.chat_conversations
        FOR INSERT TO healthops_app
        WITH CHECK (
            organization_id = healthops.current_organization_id()
            AND created_by_user_id = healthops.current_user_id()
        )
        """,

        """
        CREATE POLICY chat_conversations_update
        ON healthops.chat_conversations
        FOR UPDATE TO healthops_app
        USING (
            organization_id = healthops.current_organization_id()
            AND (
                created_by_user_id = healthops.current_user_id()
                OR healthops.current_membership_role()
                    IN ('owner', 'admin')
            )
        )
        WITH CHECK (
            organization_id = healthops.current_organization_id()
            AND (
                created_by_user_id = healthops.current_user_id()
                OR healthops.current_membership_role()
                    IN ('owner', 'admin')
            )
        )
        """,

        """
        CREATE POLICY chat_conversations_delete
        ON healthops.chat_conversations
        FOR DELETE TO healthops_app
        USING (
            organization_id = healthops.current_organization_id()
            AND (
                created_by_user_id = healthops.current_user_id()
                OR healthops.current_membership_role()
                    IN ('owner', 'admin')
            )
        )
        """,

        # ---------------------------------------------------------
        # 16. Message policies
        # ---------------------------------------------------------

        """
        CREATE POLICY chat_messages_read
        ON healthops.chat_messages
        FOR SELECT TO healthops_app
        USING (
            organization_id = healthops.current_organization_id()
            AND EXISTS (
                SELECT 1
                FROM healthops.chat_conversations AS c
                WHERE c.id = chat_messages.conversation_id
                  AND c.organization_id =
                      chat_messages.organization_id
                  AND (
                      c.created_by_user_id =
                          healthops.current_user_id()
                      OR healthops.current_membership_role()
                          IN ('owner', 'admin')
                  )
            )
        )
        """,

        """
        CREATE POLICY chat_messages_insert
        ON healthops.chat_messages
        FOR INSERT TO healthops_app
        WITH CHECK (
            organization_id = healthops.current_organization_id()
            AND EXISTS (
                SELECT 1
                FROM healthops.chat_conversations AS c
                WHERE c.id = chat_messages.conversation_id
                  AND c.organization_id =
                      chat_messages.organization_id
                  AND (
                      c.created_by_user_id =
                          healthops.current_user_id()
                      OR healthops.current_membership_role()
                          IN ('owner', 'admin')
                  )
            )
        )
        """,

        # ---------------------------------------------------------
        # 17. Dashboard policies
        # ---------------------------------------------------------

        """
        CREATE POLICY saved_dashboards_read_org
        ON healthops.saved_dashboards
        FOR SELECT TO healthops_app
        USING (
            organization_id = healthops.current_organization_id()
        )
        """,

        """
        CREATE POLICY saved_dashboards_write_analyst
        ON healthops.saved_dashboards
        FOR ALL TO healthops_app
        USING (
            organization_id = healthops.current_organization_id()
            AND healthops.current_membership_role()
                IN ('owner', 'admin', 'analyst')
        )
        WITH CHECK (
            organization_id = healthops.current_organization_id()
            AND healthops.current_membership_role()
                IN ('owner', 'admin', 'analyst')
        )
        """,

        # ---------------------------------------------------------
        # 18. Cleaning run policies
        # ---------------------------------------------------------

        """
        CREATE POLICY cleaning_runs_read_org
        ON healthops.cleaning_runs
        FOR SELECT TO healthops_app
        USING (
            organization_id = healthops.current_organization_id()
        )
        """,

        """
        CREATE POLICY cleaning_runs_write_analyst
        ON healthops.cleaning_runs
        FOR ALL TO healthops_app
        USING (
            organization_id = healthops.current_organization_id()
            AND healthops.current_membership_role()
                IN ('owner', 'admin', 'analyst')
        )
        WITH CHECK (
            organization_id = healthops.current_organization_id()
            AND healthops.current_membership_role()
                IN ('owner', 'admin', 'analyst')
        )
        """,

        # ---------------------------------------------------------
        # 19. Append-only audit event policies
        # ---------------------------------------------------------

        """
        CREATE POLICY audit_events_read_org
        ON healthops.audit_events
        FOR SELECT TO healthops_app
        USING (
            organization_id = healthops.current_organization_id()
        )
        """,

        """
        CREATE POLICY audit_events_insert_org
        ON healthops.audit_events
        FOR INSERT TO healthops_app
        WITH CHECK (
            organization_id = healthops.current_organization_id()
            AND actor_user_id = healthops.current_user_id()
        )
        """,
    ]

    _execute(statements)


def downgrade() -> None:
    statements = [
        # Drop all RLS policies before dropping their helper functions.
        "DROP POLICY IF EXISTS organizations_read_current ON healthops.organizations",
        "DROP POLICY IF EXISTS users_read_self ON healthops.users",
        "DROP POLICY IF EXISTS users_update_self ON healthops.users",
        "DROP POLICY IF EXISTS memberships_read_self_or_admin ON healthops.memberships",

        "DROP POLICY IF EXISTS workspaces_read_org ON healthops.workspaces",
        "DROP POLICY IF EXISTS workspaces_write_admin ON healthops.workspaces",

        "DROP POLICY IF EXISTS data_sources_read_org ON healthops.data_sources",
        "DROP POLICY IF EXISTS data_sources_write_analyst ON healthops.data_sources",

        "DROP POLICY IF EXISTS datasets_read_org ON healthops.datasets",
        "DROP POLICY IF EXISTS datasets_write_analyst ON healthops.datasets",

        "DROP POLICY IF EXISTS dataset_versions_read_org ON healthops.dataset_versions",
        "DROP POLICY IF EXISTS dataset_versions_insert_analyst ON healthops.dataset_versions",

        "DROP POLICY IF EXISTS analysis_jobs_read_org ON healthops.analysis_jobs",
        "DROP POLICY IF EXISTS analysis_jobs_insert_org ON healthops.analysis_jobs",
        "DROP POLICY IF EXISTS analysis_jobs_update_org ON healthops.analysis_jobs",
        "DROP POLICY IF EXISTS analysis_jobs_delete_admin ON healthops.analysis_jobs",

        "DROP POLICY IF EXISTS chat_conversations_read ON healthops.chat_conversations",
        "DROP POLICY IF EXISTS chat_conversations_insert ON healthops.chat_conversations",
        "DROP POLICY IF EXISTS chat_conversations_update ON healthops.chat_conversations",
        "DROP POLICY IF EXISTS chat_conversations_delete ON healthops.chat_conversations",

        "DROP POLICY IF EXISTS chat_messages_read ON healthops.chat_messages",
        "DROP POLICY IF EXISTS chat_messages_insert ON healthops.chat_messages",

        "DROP POLICY IF EXISTS saved_dashboards_read_org ON healthops.saved_dashboards",
        "DROP POLICY IF EXISTS saved_dashboards_write_analyst ON healthops.saved_dashboards",

        "DROP POLICY IF EXISTS cleaning_runs_read_org ON healthops.cleaning_runs",
        "DROP POLICY IF EXISTS cleaning_runs_write_analyst ON healthops.cleaning_runs",

        "DROP POLICY IF EXISTS audit_events_read_org ON healthops.audit_events",
        "DROP POLICY IF EXISTS audit_events_insert_org ON healthops.audit_events",

        # Remove FORCE from tenant-owned tables.
        "ALTER TABLE healthops.workspaces NO FORCE ROW LEVEL SECURITY",
        "ALTER TABLE healthops.data_sources NO FORCE ROW LEVEL SECURITY",
        "ALTER TABLE healthops.datasets NO FORCE ROW LEVEL SECURITY",
        "ALTER TABLE healthops.dataset_versions NO FORCE ROW LEVEL SECURITY",
        "ALTER TABLE healthops.analysis_jobs NO FORCE ROW LEVEL SECURITY",
        "ALTER TABLE healthops.chat_conversations NO FORCE ROW LEVEL SECURITY",
        "ALTER TABLE healthops.chat_messages NO FORCE ROW LEVEL SECURITY",
        "ALTER TABLE healthops.saved_dashboards NO FORCE ROW LEVEL SECURITY",
        "ALTER TABLE healthops.cleaning_runs NO FORCE ROW LEVEL SECURITY",
        "ALTER TABLE healthops.audit_events NO FORCE ROW LEVEL SECURITY",

        # Disable RLS table by table.
        "ALTER TABLE healthops.organizations DISABLE ROW LEVEL SECURITY",
        "ALTER TABLE healthops.users DISABLE ROW LEVEL SECURITY",
        "ALTER TABLE healthops.memberships DISABLE ROW LEVEL SECURITY",
        "ALTER TABLE healthops.user_sessions DISABLE ROW LEVEL SECURITY",
        "ALTER TABLE healthops.workspaces DISABLE ROW LEVEL SECURITY",
        "ALTER TABLE healthops.data_sources DISABLE ROW LEVEL SECURITY",
        "ALTER TABLE healthops.datasets DISABLE ROW LEVEL SECURITY",
        "ALTER TABLE healthops.dataset_versions DISABLE ROW LEVEL SECURITY",
        "ALTER TABLE healthops.analysis_jobs DISABLE ROW LEVEL SECURITY",
        "ALTER TABLE healthops.chat_conversations DISABLE ROW LEVEL SECURITY",
        "ALTER TABLE healthops.chat_messages DISABLE ROW LEVEL SECURITY",
        "ALTER TABLE healthops.saved_dashboards DISABLE ROW LEVEL SECURITY",
        "ALTER TABLE healthops.cleaning_runs DISABLE ROW LEVEL SECURITY",
        "ALTER TABLE healthops.audit_events DISABLE ROW LEVEL SECURITY",

        # Restore original default privileges.
        """
        ALTER DEFAULT PRIVILEGES
        FOR ROLE healthops_owner
        IN SCHEMA healthops
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO healthops_app
        """,

        """
        ALTER DEFAULT PRIVILEGES
        FOR ROLE healthops_owner
        IN SCHEMA healthops
        GRANT USAGE, SELECT ON SEQUENCES TO healthops_app
        """,

        # Restore the original table grants.
        """
        GRANT SELECT, INSERT, UPDATE, DELETE
        ON ALL TABLES IN SCHEMA healthops
        TO healthops_app
        """,

        """
        GRANT USAGE, SELECT
        ON ALL SEQUENCES IN SCHEMA healthops
        TO healthops_app
        """,

        # Drop functions and protected context storage.
        "DROP FUNCTION IF EXISTS healthops.open_tenant_context(text, uuid)",
        "DROP FUNCTION IF EXISTS healthops.resolve_session(text)",
        "DROP FUNCTION IF EXISTS healthops.current_membership_role()",
        "DROP FUNCTION IF EXISTS healthops.current_user_id()",
        "DROP FUNCTION IF EXISTS healthops.current_organization_id()",
        "DROP TABLE IF EXISTS healthops.auth_context",
    ]

    _execute(statements)
