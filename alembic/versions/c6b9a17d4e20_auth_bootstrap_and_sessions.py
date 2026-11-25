"""Add first-owner bootstrap and password-verified sessions."""
from alembic import op

revision = "c6b9a17d4e20"
down_revision = "9fa01f884e8f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # workspaces has FORCE ROW LEVEL SECURITY. The SECURITY DEFINER bootstrap
    # function runs as healthops_owner, so provide that NOLOGIN role its own
    # narrowly scoped policy for provisioning the initial workspace.
    op.execute("""
        CREATE POLICY workspaces_owner_provisioning
        ON healthops.workspaces
        FOR ALL TO healthops_owner
        USING (true)
        WITH CHECK (true)
    """)

    op.execute("""
        CREATE OR REPLACE FUNCTION healthops.auth_setup_required()
        RETURNS boolean
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path TO 'pg_catalog', 'pg_temp'
        AS $function$
            SELECT NOT EXISTS (SELECT 1 FROM healthops.users)
        $function$
    """)

    op.execute("""
        CREATE OR REPLACE FUNCTION healthops.bootstrap_first_owner(
            p_email text,
            p_email_normalized text,
            p_display_name text,
            p_password_hash text,
            p_organization_name text,
            p_organization_slug text,
            p_token_hash text,
            p_expires_at timestamptz
        )
        RETURNS TABLE(user_id uuid, organization_id uuid, workspace_id uuid)
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path TO 'pg_catalog', 'pg_temp'
        AS $function$
        DECLARE
            v_user_id uuid;
            v_organization_id uuid;
            v_workspace_id uuid;
        BEGIN
            IF p_email IS NULL OR length(trim(p_email)) = 0
               OR p_email_normalized IS NULL
               OR p_email_normalized <> lower(trim(p_email))
               OR p_display_name IS NULL OR length(trim(p_display_name)) < 2
               OR p_password_hash IS NULL OR length(p_password_hash) < 40
               OR p_organization_name IS NULL
               OR length(trim(p_organization_name)) < 2
               OR p_organization_slug IS NULL
               OR (p_organization_slug !~ '^[a-z0-9][a-z0-9-]{0,78}[a-z0-9]$'
                   AND p_organization_slug !~ '^[a-z0-9]$')
               OR p_token_hash IS NULL OR p_token_hash !~ '^[0-9a-f]{64}$'
               OR p_expires_at IS NULL
               OR p_expires_at <= pg_catalog.clock_timestamp()
               OR p_expires_at > pg_catalog.clock_timestamp() + interval '1 day'
            THEN
                RAISE EXCEPTION 'Invalid initial account details'
                    USING ERRCODE = '22023';
            END IF;

            LOCK TABLE healthops.users IN EXCLUSIVE MODE;

            IF EXISTS (SELECT 1 FROM healthops.users)
               OR EXISTS (SELECT 1 FROM healthops.organizations)
            THEN
                RAISE EXCEPTION 'Initial setup has already been completed'
                    USING ERRCODE = '55000';
            END IF;

            INSERT INTO healthops.organizations (slug, name, status)
            VALUES (p_organization_slug, trim(p_organization_name), 'active')
            RETURNING id INTO v_organization_id;

            INSERT INTO healthops.users (
                email, email_normalized, display_name, password_hash, is_active
            )
            VALUES (
                trim(p_email), p_email_normalized, trim(p_display_name),
                p_password_hash, TRUE
            )
            RETURNING id INTO v_user_id;

            INSERT INTO healthops.memberships (
                organization_id, user_id, role, status
            )
            VALUES (v_organization_id, v_user_id, 'owner', 'active');

            INSERT INTO healthops.workspaces (
                organization_id, name, slug, created_by_user_id
            )
            VALUES (
                v_organization_id, 'General Workspace', 'general', v_user_id
            )
            RETURNING id INTO v_workspace_id;

            INSERT INTO healthops.user_sessions (user_id, token_hash, expires_at)
            VALUES (v_user_id, p_token_hash, p_expires_at);

            RETURN QUERY SELECT v_user_id, v_organization_id, v_workspace_id;
        END;
        $function$
    """)

    op.execute("""
        CREATE OR REPLACE FUNCTION healthops.lookup_login(p_email_normalized text)
        RETURNS TABLE(user_id uuid, password_hash text, is_active boolean)
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path TO 'pg_catalog', 'pg_temp'
        AS $function$
            SELECT u.id, u.password_hash::text, u.is_active
            FROM healthops.users AS u
            WHERE u.email_normalized = lower(trim(p_email_normalized))
            LIMIT 1
        $function$
    """)

    op.execute("""
        CREATE OR REPLACE FUNCTION healthops.create_session_for_user(
            p_user_id uuid,
            p_password_hash text,
            p_token_hash text,
            p_expires_at timestamptz
        )
        RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path TO 'pg_catalog', 'pg_temp'
        AS $function$
        BEGIN
            IF p_token_hash IS NULL OR p_token_hash !~ '^[0-9a-f]{64}$'
               OR p_expires_at IS NULL
               OR p_expires_at <= pg_catalog.clock_timestamp()
               OR p_expires_at > pg_catalog.clock_timestamp() + interval '1 day'
            THEN
                RAISE EXCEPTION 'Invalid session details' USING ERRCODE = '22023';
            END IF;

            IF NOT EXISTS (
                SELECT 1 FROM healthops.users AS u
                WHERE u.id = p_user_id
                  AND u.password_hash = p_password_hash
                  AND u.is_active = TRUE
                  AND EXISTS (
                      SELECT 1
                      FROM healthops.memberships AS m
                      JOIN healthops.organizations AS o ON o.id = m.organization_id
                      WHERE m.user_id = u.id
                        AND m.status = 'active'
                        AND o.status = 'active'
                  )
            ) THEN
                RAISE EXCEPTION 'Email or password is incorrect' USING ERRCODE = '28000';
            END IF;

            INSERT INTO healthops.user_sessions (user_id, token_hash, expires_at)
            VALUES (p_user_id, p_token_hash, p_expires_at);
        END;
        $function$
    """)

    op.execute("""
        CREATE OR REPLACE FUNCTION healthops.revoke_session(p_token_hash text)
        RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path TO 'pg_catalog', 'pg_temp'
        AS $function$
        BEGIN
            IF p_token_hash IS NULL OR p_token_hash !~ '^[0-9a-f]{64}$' THEN
                RETURN;
            END IF;

            UPDATE healthops.user_sessions
            SET revoked_at = pg_catalog.clock_timestamp()
            WHERE token_hash = p_token_hash AND revoked_at IS NULL;
        END;
        $function$
    """)

    for signature in (
        "healthops.auth_setup_required()",
        "healthops.bootstrap_first_owner(text,text,text,text,text,text,text,timestamptz)",
        "healthops.lookup_login(text)",
        "healthops.create_session_for_user(uuid,text,text,timestamptz)",
        "healthops.revoke_session(text)",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO healthops_app")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS healthops.revoke_session(text)")
    op.execute(
        "DROP FUNCTION IF EXISTS "
        "healthops.create_session_for_user(uuid,text,text,timestamptz)"
    )
    op.execute("DROP FUNCTION IF EXISTS healthops.lookup_login(text)")
    op.execute(
        "DROP FUNCTION IF EXISTS "
        "healthops.bootstrap_first_owner(text,text,text,text,text,text,text,timestamptz)"
    )
    op.execute("DROP FUNCTION IF EXISTS healthops.auth_setup_required()")
    op.execute(
        "DROP POLICY IF EXISTS workspaces_owner_provisioning "
        "ON healthops.workspaces"
    )